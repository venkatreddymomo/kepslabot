"""KB scraper subprocess management + same-origin reverse proxy.

Relocated verbatim from main.py (behavior-preserving). The scraper runs as a
child process on its own port; these helpers start/stop it and proxy requests
under ``/kb-scraper/*`` so the admin UI can embed it on the same origin.
"""
import asyncio
import os
import subprocess
import sys as _sys
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

# Repo root (this module lives in <root>/bootstrap/, so go up one level).
_BASE_DIR = Path(__file__).resolve().parent.parent
_SCRAPER_DIR = (_BASE_DIR / "Kepsla-hotal-kb-scraper-main").resolve()
_SCRAPER_MAIN_FILE = (_SCRAPER_DIR / "main.py").resolve()
_PROXY_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
_SCRAPER_PROCESS: "subprocess.Popen[bytes] | None" = None
_SCRAPER_START_LOCK = asyncio.Lock()


def _parse_dotenv_file(env_path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE dotenv file without expanding variables."""
    parsed: dict[str, str] = {}
    if not env_path.exists():
        return parsed

    try:
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("﻿")
            value = value.strip()
            if not key:
                continue
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            parsed[key] = value
    except Exception:
        return {}

    return parsed


def _build_scraper_env() -> dict[str, str]:
    """Build environment for scraper process using current env + root .env."""
    merged = dict(os.environ)
    root_env_values = _parse_dotenv_file(_BASE_DIR / ".env")

    # Keep explicit shell env values highest priority.
    for key, value in root_env_values.items():
        merged.setdefault(key, value)

    merged.setdefault("PYTHONUNBUFFERED", "1")
    return merged


def _resolve_scraper_upstream() -> tuple[str, int]:
    """Resolve scraper upstream host/port from env and .env defaults."""
    env_map = _build_scraper_env()
    host = str(env_map.get("SCRAPER_UPSTREAM_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    raw_port = str(env_map.get("APP_PORT") or "8501").strip()
    try:
        port = int(raw_port)
    except Exception:
        port = 8501
    return host, port


def _is_scraper_start_request(method: str, target_path: str) -> bool:
    """Return True when request should explicitly start a crawl worker."""
    return method.upper() == "POST" and target_path.strip("/").lower() == "api/scrape"


async def _wait_for_scraper_ready(timeout_seconds: float = 20.0) -> bool:
    """Wait until scraper health endpoint responds after boot."""
    upstream_host, upstream_port = _resolve_scraper_upstream()
    deadline = perf_counter() + timeout_seconds
    while perf_counter() < deadline:
        process = _SCRAPER_PROCESS
        if process is not None and process.poll() is not None:
            return False

        try:
            async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as client:
                response = await client.get(f"http://{upstream_host}:{upstream_port}/health")
            if response.status_code < 500:
                return True
        except Exception:
            pass

        await asyncio.sleep(0.35)

    return False


async def _ensure_scraper_process_running() -> bool:
    """Ensure scraper process is running (lazy-start path)."""
    global _SCRAPER_PROCESS

    async with _SCRAPER_START_LOCK:
        if _SCRAPER_PROCESS is not None and _SCRAPER_PROCESS.poll() is None:
            return True

        _SCRAPER_PROCESS = _start_scraper_process()
        if _SCRAPER_PROCESS is None:
            return False

        return await _wait_for_scraper_ready()


def _rewrite_scraper_payload(content_type: str, payload: bytes) -> bytes:
    """Rewrite absolute scraper asset/api URLs so iframe stays on same origin/port."""
    lowered = str(content_type or "").lower()
    if "text/html" not in lowered and "javascript" not in lowered:
        return payload
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    text = text.replace('href="/static/', 'href="/kb-scraper/static/')
    text = text.replace('src="/static/', 'src="/kb-scraper/static/')
    text = text.replace('"/api/', '"/kb-scraper/api/')
    text = text.replace("'/api/", "'/kb-scraper/api/")
    text = text.replace("`/api/", "`/kb-scraper/api/")
    return text.encode("utf-8")


async def proxy_to_kb_scraper(request: Request, proxy_path: str = "") -> Response:
    """Proxy request to KB scraper process while preserving same-origin frontend flow."""
    upstream_host, upstream_port = _resolve_scraper_upstream()
    target_path = str(proxy_path or "").lstrip("/")
    upstream_url = f"http://{upstream_host}:{upstream_port}/{target_path}" if target_path else f"http://{upstream_host}:{upstream_port}/"
    query = str(request.url.query or "").strip()
    if query:
        upstream_url = f"{upstream_url}?{query}"

    upstream_headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower_key = key.lower()
        if lower_key in {"host", "content-length"}:
            continue
        upstream_headers[key] = value

    client_host = request.client.host if request.client else ""
    if client_host:
        prior_forwarded = upstream_headers.get("X-Forwarded-For", "").strip()
        upstream_headers["X-Forwarded-For"] = f"{prior_forwarded}, {client_host}".strip(", ")

    body = await request.body()
    async def _dispatch_upstream() -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            return await client.request(
                method=request.method,
                url=upstream_url,
                headers=upstream_headers,
                content=body,
            )

    try:
        upstream_response = await _dispatch_upstream()
    except Exception as exc:
        if _is_scraper_start_request(request.method, target_path):
            started = await _ensure_scraper_process_running()
            if not started:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "KB scraper failed to start for crawl request.",
                    },
                )
            try:
                upstream_response = await _dispatch_upstream()
            except Exception as retry_exc:
                return JSONResponse(
                    status_code=502,
                    content={
                        "detail": f"KB scraper upstream unavailable at {upstream_host}:{upstream_port}",
                        "error": str(retry_exc),
                    },
                )
        else:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "KB scraper is idle. Click Start Crawl to launch it.",
                    "upstream": f"{upstream_host}:{upstream_port}",
                    "error": str(exc),
                },
            )

    response_headers: dict[str, str] = {}
    for key, value in upstream_response.headers.items():
        lower_key = key.lower()
        if lower_key in _PROXY_HOP_BY_HOP_HEADERS:
            continue
        if lower_key == "location":
            if value.startswith("/"):
                value = f"/kb-scraper{value}"
            elif value.startswith(f"http://{upstream_host}:{upstream_port}/"):
                value = value.replace(
                    f"http://{upstream_host}:{upstream_port}",
                    "",
                    1,
                )
                if not value.startswith("/kb-scraper"):
                    value = f"/kb-scraper{value}"
        response_headers[key] = value

    content_type = upstream_response.headers.get("content-type", "")
    payload = _rewrite_scraper_payload(content_type, upstream_response.content)

    return Response(
        content=payload,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


def _start_scraper_process() -> "subprocess.Popen[bytes] | None":
    """Start KB scraper API as a child process managed by root main.py."""
    if not _SCRAPER_MAIN_FILE.exists():
        print(f"KB scraper entrypoint not found at {_SCRAPER_MAIN_FILE}; skipping auto-start.")
        return None

    try:
        scraper_env = _build_scraper_env()
        scraper_env["APP_ENV"] = "production"
        scraper_env["PYTHONUNBUFFERED"] = "1"

        # Use venv Python if available — scraper dependencies (scrapling etc.) live there.
        venv_python = _BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            venv_python = _BASE_DIR / ".venv" / "bin" / "python"
        scraper_python = str(venv_python) if venv_python.exists() else _sys.executable

        # Resolve the port from env so it matches the scraper's settings.
        _, scraper_port = _resolve_scraper_upstream()

        # Launch uvicorn directly — bypasses the scraper's __main__ block which
        # would enable watchfiles reload based on APP_ENV, causing duplicate port binds.
        process = subprocess.Popen(
            [scraper_python, "-m", "uvicorn", "main:app",
             "--host", "0.0.0.0",
             "--port", str(scraper_port),
             "--log-level", "info"],
            cwd=str(_SCRAPER_DIR),
            env=scraper_env,
        )
        print(f"KB scraper started (pid={process.pid}) on port {scraper_port} using {scraper_python}")
        return process
    except Exception as exc:
        print(f"KB scraper auto-start failed: {exc}")
        return None


def _stop_scraper_process(process: "subprocess.Popen[bytes] | None") -> None:
    """Terminate KB scraper process started by this main process."""
    global _SCRAPER_PROCESS
    if process is None:
        return
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=10)
        print("KB scraper stopped")
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:
        pass
    finally:
        if _SCRAPER_PROCESS is process:
            _SCRAPER_PROCESS = None


def start_scraper_process() -> "subprocess.Popen[bytes] | None":
    """Public entrypoint: eager-start the scraper and record the module handle."""
    global _SCRAPER_PROCESS
    _SCRAPER_PROCESS = _start_scraper_process()
    return _SCRAPER_PROCESS


def stop_scraper_process() -> None:
    """Public entrypoint: stop the module-owned scraper process, if any."""
    _stop_scraper_process(_SCRAPER_PROCESS)
