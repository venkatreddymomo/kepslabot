"""
Prompt Seed Service

Syncs local prompt markdown files (the "source of truth" for industry defaults)
into the DB registry on startup.

File layout:
    prompts/defaults/<industry>/<key>.md

File format (YAML-ish frontmatter followed by the prompt body):
    ---
    key: orchestrator.service_router
    description: Routes user message to the matching service
    variables: [service_guide]
    ---
    <prompt text on all following lines, LF only>

Rules:
- Industry-default rows (hotel_id NULL) are rewritten whenever the file's content
  hash differs from `seeded_from_file_hash` on the DB row. Safe to re-run on
  every startup.
- Hotel override rows (hotel_id set) are NEVER touched by the seed — they win
  at resolution time, so seed changes propagate only to hotels without overrides.
- A file present on disk but missing from DB is inserted. A DB row with no file
  is left alone (e.g. admin-created custom keys).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from services.prompt_registry_service import (
    SUPPORTED_INDUSTRIES,
    prompt_registry,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "defaults"


@dataclass
class ParsedPromptFile:
    industry: str
    key: str
    content: str
    description: Optional[str]
    variables: List[str]
    file_hash: str
    path: Path


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_frontmatter_value(raw: str) -> object:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip().strip("'\"") for p in inner.split(",")]
        return [p for p in parts if p]
    if raw.lower() in {"null", "none", "~"}:
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def _parse_prompt_file(path: Path) -> Optional[ParsedPromptFile]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("prompt_seed: failed to read %s: %s", path, exc)
        return None

    # Normalize newlines so the hash is stable across Windows/Unix checkouts.
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logger.warning("prompt_seed: missing frontmatter in %s", path)
        return None

    fm_block, body = match.group(1), match.group(2)
    meta: Dict[str, object] = {}
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        meta[name.strip().lower()] = _parse_frontmatter_value(value)

    key = str(meta.get("key") or "").strip()
    if not key:
        logger.warning("prompt_seed: frontmatter missing 'key' in %s", path)
        return None

    industry = path.parent.name.strip().lower()
    description_raw = meta.get("description")
    variables_raw = meta.get("variables")

    description = str(description_raw).strip() if isinstance(description_raw, str) else None
    variables = [str(v).strip() for v in variables_raw] if isinstance(variables_raw, list) else []

    # Strip one leading blank line if present, then preserve the rest verbatim.
    body = body.lstrip("\n")
    # Trim a single trailing newline only — preserve intentional trailing blanks.
    if body.endswith("\n"):
        body = body[:-1]

    return ParsedPromptFile(
        industry=industry,
        key=key,
        content=body,
        description=description,
        variables=variables,
        file_hash=_compute_hash(body),
        path=path,
    )


def _discover_files(root: Path) -> List[ParsedPromptFile]:
    if not root.exists():
        logger.warning("prompt_seed: prompts directory does not exist: %s", root)
        return []
    parsed: List[ParsedPromptFile] = []
    for industry_dir in sorted(root.iterdir()):
        if not industry_dir.is_dir():
            continue
        industry = industry_dir.name.strip().lower()
        if industry not in SUPPORTED_INDUSTRIES:
            logger.info("prompt_seed: skipping unsupported industry dir %s", industry_dir)
            continue
        for md_path in sorted(industry_dir.glob("*.md")):
            entry = _parse_prompt_file(md_path)
            if entry is not None:
                parsed.append(entry)
    return parsed


async def seed_prompts_from_files(
    root: Path = PROMPTS_DIR,
) -> Tuple[int, int, int, List[str]]:
    """
    Sync every .md file under `root` into the DB registry as an industry default row.
    Returns (inserted, updated, unchanged, updated_keys).

    `updated_keys` lists prompt_keys that actually changed in this run (inserted
    OR content-updated). Callers use it to trigger downstream invalidation —
    e.g. service-writer templates flowing into per-service generated_system_prompt.

    Performance: this runs at every startup, so it must be cheap. We do ONE
    batched SELECT for all industry-default rows we care about, compare hashes
    in memory, and only emit writes for rows that actually differ. Over a
    high-latency VPN connection this turns 19 round-trips (~45s+) into 1
    SELECT plus N writes only when files have changed (~1s on a clean run).
    """
    parsed = _discover_files(root)
    if not parsed:
        return 0, 0, 0, []

    inserted = 0
    updated = 0
    unchanged = 0
    updated_keys: List[str] = []

    from models.database import AsyncSessionLocal, PromptRegistry
    from sqlalchemy import and_, select

    industries = sorted({entry.industry for entry in parsed})

    # ── Single batch read of ALL industry-default rows we might care about ──
    # One SELECT instead of one-per-prompt — critical for slow networks.
    try:
        async with AsyncSessionLocal() as session:
            stmt = select(PromptRegistry).where(
                and_(
                    PromptRegistry.industry.in_(industries),
                    PromptRegistry.hotel_id.is_(None),
                )
            )
            result = await session.execute(stmt)
            existing_rows = result.scalars().all()
    except Exception as exc:
        logger.exception("prompt_seed: batch fetch failed: %s", exc)
        return 0, 0, 0, []

    by_industry_key: Dict[Tuple[str, str], object] = {
        (str(r.industry or "").strip().lower(), str(r.prompt_key or "").strip()): r
        for r in existing_rows
    }

    # ── Apply writes only for rows that don't match the file hash ───────────
    for entry in parsed:
        try:
            row = by_industry_key.get((entry.industry, entry.key))

            if row is None:
                # New row — INSERT via the registry helper (handles relationships).
                await prompt_registry.upsert(
                    entry.key,
                    entry.content,
                    industry=entry.industry,
                    variables=entry.variables or None,
                    description=entry.description,
                    updated_by="seed",
                    seeded_from_file_hash=entry.file_hash,
                )
                inserted += 1
                updated_keys.append(entry.key)
                continue

            existing_hash = str(getattr(row, "seeded_from_file_hash", "") or "")
            existing_content = str(getattr(row, "content", "") or "")

            if existing_hash == entry.file_hash and existing_content == entry.content:
                unchanged += 1
                continue

            # Content drifted — rewrite via upsert so version bumps and cache invalidates.
            await prompt_registry.upsert(
                entry.key,
                entry.content,
                industry=entry.industry,
                variables=entry.variables or None,
                description=entry.description,
                updated_by="seed",
                seeded_from_file_hash=entry.file_hash,
            )
            updated += 1
            updated_keys.append(entry.key)

        except Exception as exc:
            logger.exception("prompt_seed: failed to seed %s/%s: %s", entry.industry, entry.key, exc)

    # Clear cache so newly seeded content is immediately visible.
    prompt_registry._invalidate_all()  # noqa: SLF001

    logger.info(
        "prompt_seed: inserted=%d updated=%d unchanged=%d (scanned=%d files under %s)",
        inserted,
        updated,
        unchanged,
        len(parsed),
        root,
    )
    return inserted, updated, unchanged, updated_keys


__all__ = ["seed_prompts_from_files", "PROMPTS_DIR"]
