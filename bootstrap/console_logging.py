"""Windows console hardening + app console logging setup.

Relocated verbatim from main.py (behavior-preserving). Import this module and
call ``install_platform_hardening()`` BEFORE any third-party import so the WMI
hang fix and the safe stdout/stderr wrappers are active early.
"""
import logging as _logging
import sys as _sys

# Logger for API call lines — shares the same channel as uvicorn's INFO logs
# so it's never silently dropped by _SafeStream on Windows.
_api_log = _logging.getLogger("app.api")


class _SafeStream:
    """Drop-in stdout/stderr wrapper that silently swallows OSError."""

    def __init__(self, stream):
        self._inner = stream

    def write(self, s):
        try:
            return self._inner.write(s)
        except OSError:
            return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except OSError:
            pass

    def isatty(self):
        try:
            return self._inner.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install_platform_hardening() -> None:
    """Disable the Py3.13 WMI hang and wrap stdout/stderr against Errno-22.

    On Windows the console handle can become invalid causing OSError [Errno 22]
    on any print/write to stdout/stderr, which crashes the entire request.

    Py 3.13's platform.win32_ver() invokes a WMI query that can hang for
    minutes on slow/locked WMI services. Both watchfiles (uvicorn reloader)
    and sqlalchemy import-time call this transitively, freezing startup with
    no escape from Ctrl+C. Setting platform._wmi = None makes _wmi_query raise
    OSError immediately. Must run before any third-party import.
    """
    if _sys.platform == "win32":
        try:
            import platform as _platform
            _platform._wmi = None
        except Exception:
            pass

    try:
        if _sys.platform == "win32":
            _sys.stdout = _SafeStream(_sys.stdout)
            _sys.stderr = _SafeStream(_sys.stderr)
    except Exception:
        pass


def configure_console_logging() -> None:
    """Restore terminal logging: app request lines + warnings/errors on console.

    uvicorn only attaches a console handler to its own ``uvicorn.*`` loggers.
    The app's own loggers — including the per-request ``app.api`` lines
    (``[API] ... -> 200``) and error tracebacks — propagate to the ROOT logger,
    which has no handler by default, so those lines silently vanish. Attach a
    single stdout handler to root at INFO (idempotently), and mute known-chatty
    third-party libraries so the console stays readable.

    Safe alongside uvicorn (its loggers use ``propagate=False`` → no duplicate
    lines) and the file/DB log sinks (separate writers, unaffected).
    """
    root = _logging.getLogger()
    if not any(getattr(h, "_kepsla_console", False) for h in root.handlers):
        handler = _logging.StreamHandler(_sys.stdout)
        handler.setFormatter(_logging.Formatter("%(levelname)s:     %(message)s"))
        handler.setLevel(_logging.INFO)
        handler._kepsla_console = True  # marker → idempotent across reload imports
        root.addHandler(handler)
    if root.level == _logging.NOTSET or root.level > _logging.INFO:
        root.setLevel(_logging.INFO)
    _logging.getLogger("app.api").setLevel(_logging.INFO)
    # Keep the console signal-rich: silence libraries that spam at INFO.
    for _noisy in (
        "httpx", "httpcore", "openai", "aiomysql", "asyncio",
        "watchfiles", "watchfiles.main", "python_multipart", "multipart", "urllib3",
    ):
        _logging.getLogger(_noisy).setLevel(_logging.WARNING)


def print_api_log(method: str, path: str, query: str, status_code: int, duration_ms: float) -> None:
    """Emit compact terminal logs for /api and /admin/api calls.

    Uses Python's logging module (same channel as uvicorn INFO lines) instead of
    print() so the output is never silently dropped by _SafeStream on Windows when
    the console stdout handle becomes invalid during request handling.
    """
    path_text = str(path or "")
    is_admin_api = path_text.startswith("/admin/api/")
    is_public_api = path_text.startswith("/api/")
    if not (is_admin_api or is_public_api):
        return
    target = f"{path_text}?{query}" if str(query or "").strip() else path_text
    tag = "[ADMIN_API]" if is_admin_api else "[API]"
    try:
        _api_log.info(
            "%s %s %s -> %d (%.2f ms)",
            tag, str(method or "").upper(), target, int(status_code), float(duration_ms),
        )
    except Exception:
        pass
