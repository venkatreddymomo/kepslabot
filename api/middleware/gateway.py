"""Gateway HTTP middleware + global exception handler.

Relocated verbatim from main.py (behavior-preserving). Provides trace-id
stamping, API-key auth guard, rate limiting, structured tracing across the
several backend loggers, and a JSON-always unhandled-exception handler.

Registered from main.py via::

    app.middleware("http")(gateway_middleware)
    app.exception_handler(Exception)(global_exception_handler)
"""
from pathlib import Path
from time import perf_counter

from fastapi import Request
from fastapi.responses import JSONResponse

from bootstrap.console_logging import print_api_log
from services import new_detailed_logger
from services.backend_trace_service import backend_trace_service
from services.everything_trace_service import everything_trace_service
from services.gateway_service import gateway_service
from services.observability_service import observability_service
from services.step_trace_service import step_trace_service

# Repo root is two levels up from api/middleware/gateway.py.
_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"


async def global_exception_handler(request: Request, exc: Exception):
    """Return JSON (never HTML/text) for all unhandled exceptions."""
    import traceback as _tb
    # Write full traceback to file so it's never lost
    try:
        _crash_path = _LOGS_DIR / "gateway_crash.log"
        _crash_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        with _crash_path.open("a", encoding="utf-8") as _cf:
            _cf.write(
                f"\n[{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{request.method} {request.url.path} — {exc!r}\n{_tb.format_exc()}\n"
            )
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "error_type": type(exc).__name__},
    )


async def gateway_middleware(request: Request, call_next):
    """Gateway hardening: trace-id, auth guard, and simple rate limiting."""
    trace_id = request.headers.get("x-trace-id") or observability_service.new_trace_id()
    request.state.trace_id = trace_id
    start = perf_counter()
    query_string = str(request.url.query or "")

    path = request.url.path or ""
    is_docs_or_public = path in {"/", "/health", "/api/config"} or path.startswith(
        ("/docs", "/openapi.json", "/redoc", "/static")
    )
    # Keep gateway controls on runtime chat traffic. Admin APIs stay open for
    # local operator UX (same behavior as the earlier stable project variant).
    protected_prefix = path.startswith("/api/chat")
    should_guard = protected_prefix and not is_docs_or_public
    should_trace_backend = (
        path.startswith("/api/")
        or path.startswith("/admin/api/")
        or path.startswith("/guest-journey/")
        or path.startswith("/engage-bot/")
    )

    client_host = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    step_trace_service.log_event(
        "http_request_start",
        payload={
            "query": dict(request.query_params),
            "client_host": client_host,
            "user_agent": request.headers.get("user-agent", ""),
            "should_guard": should_guard,
            "should_trace_backend": should_trace_backend,
        },
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        component="main.gateway_middleware",
        step="request_received",
        stage="start",
    )

    # -- new_detailed_logger: HTTP request in --
    try:
        new_detailed_logger.log_http_request(
            trace_id=trace_id,
            method=request.method,
            path=path,
            client_host=client_host,
            user_agent=request.headers.get("user-agent", ""),
            query=dict(request.query_params) or None,
        )
    except Exception as _ndl_exc:
        try:
            import sys as _sys
            print(f"[new_detailed_logger] HTTP request log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
        except Exception:
            pass

    if should_trace_backend:
        backend_trace_service.log_event(
            "http_request_start",
            {
                "path": path,
                "query": dict(request.query_params),
                "client_host": client_host,
                "user_agent": request.headers.get("user-agent", ""),
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            component="main.gateway_middleware",
        )
        everything_trace_service.log_event(
            "http_request_start",
            {
                "path": path,
                "query": dict(request.query_params),
                "client_host": client_host,
                "user_agent": request.headers.get("user-agent", ""),
                "headers": {
                    "x-trace-id": request.headers.get("x-trace-id", ""),
                    "x-forwarded-for": request.headers.get("x-forwarded-for", ""),
                    "referer": request.headers.get("referer", ""),
                    "origin": request.headers.get("origin", ""),
                },
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            component="main.gateway_middleware",
        )

    if should_guard:
        provided_api_key = request.headers.get("x-api-key")
        if not gateway_service.is_authorized(provided_api_key):
            step_trace_service.log_event(
                "http_request_denied_auth",
                payload={"client_host": client_host},
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                status_code=401,
                component="main.gateway_middleware",
                step="auth_check",
                stage="failed",
                error="unauthorized_api_key",
            )
            observability_service.log_event(
                "gateway_denied_auth",
                {
                    "trace_id": trace_id,
                    "path": path,
                    "method": request.method,
                },
            )
            # -- new_detailed_logger: rejected - auth --
            try:
                new_detailed_logger.log_http_rejected(
                    trace_id=trace_id,
                    reason="unauthorized_api_key",
                    path=path,
                    method=request.method,
                    client_host=client_host,
                    status_code=401,
                )
            except Exception as _ndl_exc:
                try:
                    import sys as _sys
                    print(f"[new_detailed_logger] rejected log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
                except Exception:
                    pass
            if should_trace_backend:
                backend_trace_service.log_event(
                    "http_request_denied_auth",
                    {
                        "path": path,
                        "client_host": client_host,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=401,
                    component="main.gateway_middleware",
                    error="unauthorized_api_key",
                )
                everything_trace_service.log_event(
                    "http_request_denied_auth",
                    {
                        "path": path,
                        "client_host": client_host,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=401,
                    component="main.gateway_middleware",
                    error="unauthorized_api_key",
                )
            duration_ms = round((perf_counter() - start) * 1000.0, 2)
            print_api_log(
                method=request.method,
                path=path,
                query=query_string,
                status_code=401,
                duration_ms=duration_ms,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: invalid API key.", "trace_id": trace_id},
                headers={"X-Trace-Id": trace_id},
            )

    if should_guard:
        rate_limit_key = f"{client_host}:{path}"
        allowed, retry_after = gateway_service.allow_request(rate_limit_key)
        if not allowed:
            step_trace_service.log_event(
                "http_request_rate_limited",
                payload={
                    "client_host": client_host,
                    "retry_after_seconds": retry_after,
                    "rate_limit_key": rate_limit_key,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                status_code=429,
                component="main.gateway_middleware",
                step="rate_limit_check",
                stage="failed",
                error="rate_limited",
            )
            observability_service.log_event(
                "gateway_rate_limited",
                {
                    "trace_id": trace_id,
                    "path": path,
                    "method": request.method,
                    "client_host": client_host,
                    "retry_after_seconds": retry_after,
                },
            )
            # -- new_detailed_logger: rejected - rate limited --
            try:
                new_detailed_logger.log_http_rejected(
                    trace_id=trace_id,
                    reason="rate_limited",
                    path=path,
                    method=request.method,
                    client_host=client_host,
                    status_code=429,
                    extra=f"retry_after={retry_after}s",
                )
            except Exception as _ndl_exc:
                try:
                    import sys as _sys
                    print(f"[new_detailed_logger] rate-limit log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
                except Exception:
                    pass
            if should_trace_backend:
                backend_trace_service.log_event(
                    "http_request_rate_limited",
                    {
                        "path": path,
                        "client_host": client_host,
                        "retry_after_seconds": retry_after,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=429,
                    component="main.gateway_middleware",
                    error="rate_limited",
                )
                everything_trace_service.log_event(
                    "http_request_rate_limited",
                    {
                        "path": path,
                        "client_host": client_host,
                        "retry_after_seconds": retry_after,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=429,
                    component="main.gateway_middleware",
                    error="rate_limited",
                )
            duration_ms = round((perf_counter() - start) * 1000.0, 2)
            print_api_log(
                method=request.method,
                path=path,
                query=query_string,
                status_code=429,
                duration_ms=duration_ms,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests.", "retry_after_seconds": retry_after, "trace_id": trace_id},
                headers={
                    "Retry-After": str(retry_after),
                    "X-Trace-Id": trace_id,
                },
            )

    step_trace_service.log_event(
        "http_request_dispatch",
        payload={"client_host": client_host},
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        component="main.gateway_middleware",
        step="handler_dispatch",
        stage="start",
    )
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((perf_counter() - start) * 1000.0, 2)
        step_trace_service.log_event(
            "http_request_exception",
            payload={"duration_ms": duration_ms, "client_host": client_host},
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=500,
            component="main.gateway_middleware",
            step="handler_dispatch",
            stage="failed",
            error=str(exc),
        )
        # -- Write full traceback to file so OSError/Errno-22 never hides root cause --
        try:
            import traceback as _tb
            _crash_path = _LOGS_DIR / "gateway_crash.log"
            _crash_path.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            with _crash_path.open("a", encoding="utf-8") as _cf:
                _cf.write(
                    f"\n[{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{request.method} {path} — {exc!r}\n{_tb.format_exc()}\n"
                )
        except Exception:
            pass
        observability_service.log_event(
            "gateway_unhandled_exception",
            {
                "trace_id": trace_id,
                "path": path,
                "method": request.method,
                "duration_ms": duration_ms,
                "error": str(exc),
            },
        )
        # -- new_detailed_logger: HTTP response (unhandled exception) --
        try:
            new_detailed_logger.log_http_response(
                trace_id=trace_id,
                method=request.method,
                path=path,
                status_code=500,
                duration_ms=duration_ms,
                error=str(exc),
            )
        except Exception as _ndl_exc:
            try:
                import sys as _sys
                print(f"[new_detailed_logger] exception response log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
            except Exception:
                pass
        if should_trace_backend:
            backend_trace_service.log_event(
                "http_request_exception",
                {
                    "path": path,
                    "duration_ms": duration_ms,
                    "client_host": client_host,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                component="main.gateway_middleware",
                error=str(exc),
            )
            everything_trace_service.log_event(
                "http_request_exception",
                {
                    "path": path,
                    "duration_ms": duration_ms,
                    "client_host": client_host,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                component="main.gateway_middleware",
                error=str(exc),
            )
        print_api_log(
            method=request.method,
            path=path,
            query=query_string,
            status_code=500,
            duration_ms=duration_ms,
        )
        # Return JSON error instead of re-raising (prevents bare 500 HTML responses)
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "trace_id": trace_id},
            headers={"X-Trace-Id": trace_id},
        )

    duration_ms = round((perf_counter() - start) * 1000.0, 2)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
    step_trace_service.log_event(
        "http_request_end",
        payload={"duration_ms": duration_ms, "client_host": client_host},
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        status_code=response.status_code,
        component="main.gateway_middleware",
        step="response_emitted",
        stage="completed",
    )

    # -- new_detailed_logger: HTTP response out --
    try:
        new_detailed_logger.log_http_response(
            trace_id=trace_id,
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    except Exception as _ndl_exc:
        try:
            import sys as _sys
            print(f"[new_detailed_logger] response log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
        except Exception:
            pass

    observability_service.log_event(
        "api_request",
        {
            "trace_id": trace_id,
            "path": path,
            "method": request.method,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client_host": client_host,
        },
    )
    if should_trace_backend:
        backend_trace_service.log_event(
            "http_request_end",
            {
                "path": path,
                "duration_ms": duration_ms,
                "client_host": client_host,
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=response.status_code,
            component="main.gateway_middleware",
        )
        everything_trace_service.log_event(
            "http_request_end",
            {
                "path": path,
                "duration_ms": duration_ms,
                "client_host": client_host,
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=response.status_code,
            component="main.gateway_middleware",
        )
    print_api_log(
        method=request.method,
        path=path,
        query=query_string,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response
