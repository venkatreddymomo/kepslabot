#!/usr/bin/env python
"""
Freeze one hotel's live runtime so later shared/default edits do not affect it.

What this script does:
1. Snapshots the current hotel-scoped runtime to a local backup JSON.
2. Copies every effective prompt into a hotel-specific override row.
3. Locks every existing generated service prompt for that hotel.

This is intentionally conservative:
- It does not regenerate prompts.
- It does not rewrite service forms or business config.
- It only makes the current effective state hotel-owned and harder to clobber.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "backups" / "hotel_freeze"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from models.database import AsyncSessionLocal, BotService, Hotel, WidgetDeployment, engine
from services.db_config_service import db_config_service
from services.prompt_registry_service import EffectivePrompt, prompt_registry
from services.widget_deployment_service import widget_deployment_service


@dataclass
class FreezeSummary:
    hotel_code: str
    hotel_id: int
    prompts_total: int
    prompts_copied_to_hotel_scope: int
    prompts_already_hotel_scoped: int
    services_total: int
    service_prompts_locked_now: int
    service_prompts_already_locked: int
    services_missing_generated_prompt: list[str]
    widget_deployments: int
    backup_file: str
    completed_at: str


def _serialize_effective_prompt(row: EffectivePrompt) -> dict[str, Any]:
    return {
        "key": row.key,
        "content": row.content,
        "source": row.source,
        "industry": row.industry,
        "has_override": row.has_override,
        "industry_default_content": row.industry_default_content,
        "variables": list(row.variables or []),
        "description": row.description,
        "version": row.version,
    }


async def _resolve_hotel_row(hotel_code: str) -> Hotel:
    async with AsyncSessionLocal() as session:
        hotel = (
            await session.execute(
                select(Hotel).where(func.lower(Hotel.code) == str(hotel_code).strip().lower())
            )
        ).scalar_one_or_none()
        if hotel is None:
            raise RuntimeError(f"Hotel not found for code={hotel_code!r}")
        return hotel


async def _collect_snapshot(hotel_code: str) -> dict[str, Any]:
    scope_token = db_config_service.set_hotel_context(hotel_code)
    try:
        hotel = await _resolve_hotel_row(hotel_code)
        full_config = await db_config_service.get_full_config()
        effective_prompts = await prompt_registry.list_effective_for_hotel(hotel_code)
        widget_deployments = await widget_deployment_service.list(hotel_code)
        async with AsyncSessionLocal() as session:
            service_rows = (
                await session.execute(
                    select(BotService).where(BotService.hotel_id == hotel.id).order_by(BotService.id.asc())
                )
            ).scalars().all()
            widget_rows = (
                await session.execute(
                    select(WidgetDeployment)
                    .where(WidgetDeployment.hotel_code == str(hotel_code).strip().lower())
                    .order_by(WidgetDeployment.id.asc())
                )
            ).scalars().all()
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "hotel_code": hotel.code,
            "hotel_id": hotel.id,
            "full_config": full_config,
            "effective_prompts": [_serialize_effective_prompt(row) for row in effective_prompts],
            "widget_deployments": widget_deployments,
            "service_prompt_lock_state": [
                {
                    "db_row_id": row.id,
                    "service_id": row.service_id,
                    "name": row.name,
                    "generated_system_prompt_present": bool(str(row.generated_system_prompt or "").strip()),
                    "generated_system_prompt_override": bool(row.generated_system_prompt_override),
                }
                for row in service_rows
            ],
            "widget_deployment_rows": [
                {
                    "id": row.id,
                    "widget_key": row.widget_key,
                    "hotel_code": row.hotel_code,
                    "name": row.name,
                }
                for row in widget_rows
            ],
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


async def _copy_effective_prompts_to_hotel_scope(hotel_code: str, hotel_id: int) -> tuple[int, int, int]:
    effective_prompts = await prompt_registry.list_effective_for_hotel(hotel_code)
    copied = 0
    already_hotel_scoped = 0
    for row in effective_prompts:
        if row.source == "hotel_override":
            already_hotel_scoped += 1
            continue
        await prompt_registry.upsert(
            row.key,
            row.content,
            hotel_id=hotel_id,
            variables=list(row.variables or []),
            description=row.description,
            updated_by="freeze_hotel_runtime",
        )
        copied += 1
    return len(effective_prompts), copied, already_hotel_scoped


async def _lock_generated_service_prompts(hotel_id: int) -> tuple[int, int, int, list[str]]:
    locked_now = 0
    already_locked = 0
    services_total = 0
    missing_prompt: list[str] = []
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BotService).where(BotService.hotel_id == hotel_id).order_by(BotService.id.asc())
            )
        ).scalars().all()
        services_total = len(rows)
        for row in rows:
            if bool(row.generated_system_prompt_override):
                already_locked += 1
                continue
            if not str(row.generated_system_prompt or "").strip():
                missing_prompt.append(str(row.service_id or row.name or row.id))
                continue
            row.generated_system_prompt_override = True
            locked_now += 1
        await session.commit()
    return services_total, locked_now, already_locked, missing_prompt


async def freeze_hotel_runtime(hotel_code: str, output_dir: Path) -> FreezeSummary:
    hotel_code = str(hotel_code or "").strip().lower()
    if not hotel_code:
        raise RuntimeError("hotel_code is required")

    output_dir.mkdir(parents=True, exist_ok=True)
    before = await _collect_snapshot(hotel_code)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_file = output_dir / f"{hotel_code}_{timestamp}.json"
    backup_file.write_text(json.dumps(before, indent=2, ensure_ascii=True), encoding="utf-8")

    hotel_id = int(before["hotel_id"])
    scope_token = db_config_service.set_hotel_context(hotel_code)
    try:
        prompts_total, prompts_copied, prompts_already = await _copy_effective_prompts_to_hotel_scope(
            hotel_code,
            hotel_id,
        )
        services_total, locked_now, already_locked, missing_prompt = await _lock_generated_service_prompts(
            hotel_id
        )
        widget_deployments = len(before.get("widget_deployments") or [])
    finally:
        db_config_service.reset_hotel_context(scope_token)

    summary = FreezeSummary(
        hotel_code=hotel_code,
        hotel_id=hotel_id,
        prompts_total=prompts_total,
        prompts_copied_to_hotel_scope=prompts_copied,
        prompts_already_hotel_scoped=prompts_already,
        services_total=services_total,
        service_prompts_locked_now=locked_now,
        service_prompts_already_locked=already_locked,
        services_missing_generated_prompt=missing_prompt,
        widget_deployments=widget_deployments,
        backup_file=str(backup_file),
        completed_at=datetime.now(UTC).isoformat(),
    )

    summary_file = output_dir / f"{hotel_code}_{timestamp}.freeze-summary.json"
    summary_file.write_text(json.dumps(asdict(summary), indent=2, ensure_ascii=True), encoding="utf-8")
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze a hotel's current runtime into hotel-owned scope.")
    parser.add_argument("hotel_code", help="Hotel code to freeze, for example: zone")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for backup artifacts (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


async def _async_main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        summary = await freeze_hotel_runtime(
            hotel_code=args.hotel_code,
            output_dir=Path(args.output_dir).resolve(),
        )
        print(json.dumps(asdict(summary), indent=2, ensure_ascii=True))
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
