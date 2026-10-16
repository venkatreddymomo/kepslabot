"""Tests for ConnectionManager.send_to_session (api.routes.websocket_chat).

Guards the regression where a slow orchestration coroutine sent the finished
answer to the ORIGINAL socket it captured, even after the guest reconnected
mid-turn (page reload / network blip / hotel switch in the admin harness). The
original socket is closed by then, so the answer was dropped silently and the
widget timed out with "taking longer than usual" despite a good answer existing.

send_to_session must deliver to whichever socket is live *now* for the session.
"""

import json

import pytest

from api.routes.websocket_chat import ConnectionManager


class FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket used by ConnectionManager."""

    def __init__(self, *, send_raises: bool = False) -> None:
        self.received: list[dict] = []
        self.closed_with: int | None = None
        self._send_raises = send_raises

    async def accept(self) -> None:  # noqa: D401 - test stub
        return None

    async def send_text(self, text: str) -> None:
        if self._send_raises:
            # Mirror Starlette's error when sending on a closed connection.
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')
        self.received.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


@pytest.mark.asyncio
async def test_send_to_session_targets_the_live_socket_after_reconnect() -> None:
    mgr = ConnectionManager()
    old = FakeWebSocket()
    new = FakeWebSocket()

    # Guest connects, then reconnects with the same session_id (old is replaced).
    await mgr.connect(old, "s1")
    await mgr.connect(new, "s1")

    # The handler still holds `old` (captured before orchestration). Routing by
    # session_id must land the answer on the live socket `new`, not `old`.
    delivered = await mgr.send_to_session(
        "s1", {"type": "response", "message_id": "m1", "data": {"message": "hi"}}, fallback=old
    )

    assert delivered is True
    assert len(new.received) == 1
    assert new.received[0]["message_id"] == "m1"
    # The stale captured socket must NOT receive the answer.
    assert old.received == []
    # connect() should have gracefully closed the superseded socket.
    assert old.closed_with == 1001


@pytest.mark.asyncio
async def test_send_to_session_uses_fallback_when_no_live_socket() -> None:
    mgr = ConnectionManager()
    captured = FakeWebSocket()

    # Session has no registered live socket (e.g. guest fully gone). The captured
    # socket is the only delivery option — best-effort send to it.
    delivered = await mgr.send_to_session(
        "ghost", {"type": "response", "message_id": "m2"}, fallback=captured
    )

    assert delivered is True
    assert captured.received[0]["message_id"] == "m2"


@pytest.mark.asyncio
async def test_send_to_session_no_socket_no_fallback_returns_false() -> None:
    mgr = ConnectionManager()
    delivered = await mgr.send_to_session("ghost", {"type": "response"}, fallback=None)
    assert delivered is False


@pytest.mark.asyncio
async def test_superseded_socket_disconnect_keeps_live_socket_registered() -> None:
    """The old socket's teardown must not evict the reconnected live socket.

    Otherwise A's handler finally-block (running after the guest reconnected on
    B) would flip the session offline and later session-routed sends would lose
    the live socket.
    """
    mgr = ConnectionManager()
    old = FakeWebSocket()
    new = FakeWebSocket()
    await mgr.connect(old, "s1")
    await mgr.connect(new, "s1")  # reconnect → `new` is now the live socket

    # The superseded `old` socket's handler unwinds and calls disconnect.
    mgr.disconnect("s1", old)
    assert mgr.is_connected("s1") is True
    assert mgr._connections["s1"] is new

    # When the live socket itself disconnects, it IS evicted.
    mgr.disconnect("s1", new)
    assert mgr.is_connected("s1") is False


def test_disconnect_without_websocket_is_backward_compatible() -> None:
    mgr = ConnectionManager()
    only = FakeWebSocket()
    mgr._connections["s1"] = only
    mgr.disconnect("s1")  # legacy call site, no websocket arg → evicts
    assert mgr.is_connected("s1") is False


@pytest.mark.asyncio
async def test_send_to_session_swallows_send_on_dead_socket() -> None:
    """A dead registered socket must not raise out of send_to_session.

    This is exactly the failure that previously bubbled up as
    '[WS] unexpected error ... WebSocket is not connected'.
    """
    mgr = ConnectionManager()
    dead = FakeWebSocket(send_raises=True)
    mgr._connections["s1"] = dead

    # Must not raise; returns True because a socket was found to attempt on.
    delivered = await mgr.send_to_session("s1", {"type": "response"}, fallback=None)
    assert delivered is True
    assert dead.received == []
