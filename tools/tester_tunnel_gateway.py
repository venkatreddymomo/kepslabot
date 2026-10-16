"""Filtering gateway between a public tunnel and the dev bot, so a tester can use the /chat harness
without the tunnel also exposing the admin API (which has no login: it edits every hotel's prompts,
KB, services and settings).

    cd admin_ui && npx vite build --outDir C:/Users/Hp/cloudflared/tester_chat_dist --emptyOutDir
    set GATEWAY_CHAT_DIST=C:/Users/Hp/cloudflared/tester_chat_dist
    python tools/tester_tunnel_gateway.py                    # listens on :8020, forwards to :8012
    cloudflared tunnel --url http://localhost:8020           # share https://<random>.trycloudflare.com/chat

Let through: the harness page, the chat API and WebSocket, widget endpoints, and a few read-only
admin GETs the harness calls. Everything else gets 403 and is logged, including every admin write and
the global model switch (POST /api/chat/model-override changes the model for every hotel on the server).

With GATEWAY_CHAT_DIST set, /chat and its /admin/<file> assets come from that separate build (never
admin_ui/dist, which :8011 serves) and nothing reaches the Vite dev server: no dev-server exposure, no
hot-reload client polling localhost:8080, and one bundle instead of hundreds of modules over the tunnel.
"""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from multidict import CIMultiDict

UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "http://127.0.0.1:8012").rstrip("/")
PORT = int(os.environ.get("GATEWAY_PORT", "8020"))
CHAT_DIST = Path(os.environ["GATEWAY_CHAT_DIST"]).resolve() if os.environ.get("GATEWAY_CHAT_DIST") else None

READ_ONLY = {"GET", "HEAD"}
# Read-only admin calls the harness page makes (welcome text, ticket list, booking list).
ADMIN_READS = ("/admin/api/config/onboarding/business", "/admin/api/tickets", "/admin/api/bookings")
# Dev-server modules for the harness page: Vite internals or files with an extension.
VITE_MODULE = re.compile(r"^/admin/(@|node_modules/|src/|chat\.html$)|^/admin/.+\.[A-Za-z0-9]{1,6}$")
# Never through the tunnel, whatever the route: Vite's raw file access (@fs, ?raw / ?inline / ?url -
# the vectors of the 2025 Vite file-read CVEs, some unpatched in 5.4.19 on Windows), dotfiles such as
# .env (Vite's own deps cache, node_modules/.vite/, excepted) and backslash paths.
UNSAFE_PATH = re.compile(r"@fs|\\|%5c|/\.(?!vite/)", re.I)
UNSAFE_QUERY = re.compile(r"(^|[&?])(raw|inline|url)\b", re.I)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length", "host"}

log = logging.getLogger("gateway")


def allowed(method: str, path: str, raw_path: str = "", query: str = "") -> bool:
    if UNSAFE_PATH.search(path) or UNSAFE_PATH.search(raw_path) or UNSAFE_QUERY.search(query):
        return False
    if path in ("/chat", "/chat/", "/health", "/api/health", "/favicon.ico", "/widget.js"):
        return method in READ_ONLY
    if path.startswith("/api/chat/model-override"):
        return method in READ_ONLY
    if path.startswith(("/api/chat/", "/api/widget/")):
        return True
    if path.startswith("/admin/api/"):
        return method in READ_ONLY and path.startswith(ADMIN_READS)
    if path.startswith("/admin/"):
        return method in READ_ONLY and bool(VITE_MODULE.search(path))
    if path.startswith(("/static/", "/assets/")):
        return method in READ_ONLY
    return False


def _forward_headers(request: web.Request) -> dict:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    # Keep the public host so links the bot builds point at the tunnel, not at localhost.
    headers["Host"] = request.host
    headers.setdefault("X-Forwarded-Proto", request.headers.get("X-Forwarded-Proto", request.scheme))
    return headers


async def proxy_ws(request: web.Request) -> web.WebSocketResponse:
    client_ws = web.WebSocketResponse(autoping=True, heartbeat=25)
    await client_ws.prepare(request)
    headers = {k: v for k, v in _forward_headers(request).items()
               if not k.lower().startswith("sec-websocket")}
    target = UPSTREAM.replace("http", "ws", 1) + request.path_qs
    session: ClientSession = request.app["session"]
    async with session.ws_connect(target, headers=headers, autoping=True, heartbeat=25,
                                  max_msg_size=0) as upstream_ws:

        async def pump(src, dst):
            async for msg in src:
                if msg.type == WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        tasks = [asyncio.create_task(pump(client_ws, upstream_ws)),
                 asyncio.create_task(pump(upstream_ws, client_ws))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
    await client_ws.close()
    return client_ws


def _built_file(path: str) -> Path | None:
    """The file in the separate harness build that serves this path, or None."""
    if path in ("/chat", "/chat/"):
        rel = "chat.html"
    elif path.startswith("/admin/") and not path.startswith("/admin/api/"):
        rel = path[len("/admin/"):]
    else:
        return None
    if rel in ("", "index.html"):  # the admin app itself
        return None
    candidate = (CHAT_DIST / rel).resolve()
    if CHAT_DIST not in candidate.parents or not candidate.is_file():
        return None
    return candidate


async def handle(request: web.Request) -> web.StreamResponse:
    path, method = request.path, request.method
    if CHAT_DIST is not None and method in READ_ONLY and (path in ("/chat", "/chat/") or (
            path.startswith("/admin/") and not path.startswith("/admin/api/"))):
        if UNSAFE_PATH.search(request.raw_path):
            log.info("BLOCKED %s %s", method, request.raw_path)
            return web.Response(status=403, text="Not available through the test link.")
        built = _built_file(path)
        if built is None:
            log.info("BLOCKED %s %s", method, request.raw_path)
            return web.Response(status=403, text="Not available through the test link.")
        cache = "no-cache" if built.name == "chat.html" else "public, max-age=86400, immutable"
        return web.FileResponse(built, headers={"Cache-Control": cache})
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    if is_ws:
        if path == "/ws/chat":
            return await proxy_ws(request)
        log.info("BLOCKED ws %s", path)
        return web.Response(status=403, text="Not available through the test link.")
    if not allowed(method, path, request.raw_path, request.query_string):
        log.info("BLOCKED %s %s", method, request.raw_path)
        return web.Response(status=403, text="Not available through the test link.")

    session: ClientSession = request.app["session"]
    body = await request.read()
    async with session.request(method, UPSTREAM + request.raw_path, headers=_forward_headers(request),
                               data=body if body else None, allow_redirects=False) as upstream:
        payload = await upstream.read()
        # A multidict, so several Set-Cookie headers all reach the browser.
        headers = CIMultiDict((k, v) for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP)
        return web.Response(status=upstream.status, body=payload, headers=headers)


async def _startup(app: web.Application) -> None:
    app["session"] = ClientSession(timeout=ClientTimeout(total=600), auto_decompress=False)


async def _cleanup(app: web.Application) -> None:
    await app["session"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    app.router.add_route("*", "/{tail:.*}", handle)
    log.info("gateway :%s -> %s", PORT, UPSTREAM)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
