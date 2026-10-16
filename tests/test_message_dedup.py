"""Unit tests for the chat message idempotency store (services.message_dedup).

Guards the guarantee that a retried message (same message_id) is never processed
twice, while genuinely distinct messages always are.
"""

import asyncio

import pytest

from services import message_dedup as dedup_mod
from services.message_dedup import MessageDedup


@pytest.mark.asyncio
async def test_first_claim_is_owner() -> None:
    d = MessageDedup()
    verdict, cached = await d.claim(("sess", "id-1"))
    assert verdict == "owner"
    assert cached is None


@pytest.mark.asyncio
async def test_completed_duplicate_returns_cached_result() -> None:
    d = MessageDedup()
    key = ("sess", "id-1")
    verdict, _ = await d.claim(key)
    assert verdict == "owner"
    await d.complete(key, {"message": "hello"})

    # A retry of the SAME id gets the cached answer, not a fresh process.
    verdict2, cached2 = await d.claim(key)
    assert verdict2 == "duplicate"
    assert cached2 == {"message": "hello"}


@pytest.mark.asyncio
async def test_distinct_ids_are_independent() -> None:
    d = MessageDedup()
    v1, _ = await d.claim(("sess", "id-1"))
    v2, _ = await d.claim(("sess", "id-2"))
    # Two different messages → both owners, both processed.
    assert v1 == "owner"
    assert v2 == "owner"


@pytest.mark.asyncio
async def test_same_id_different_session_is_independent() -> None:
    d = MessageDedup()
    v1, _ = await d.claim(("sess-A", "id-1"))
    v2, _ = await d.claim(("sess-B", "id-1"))
    assert v1 == "owner"
    assert v2 == "owner"


@pytest.mark.asyncio
async def test_release_if_pending_allows_retry_after_failure() -> None:
    d = MessageDedup()
    key = ("sess", "id-1")
    verdict, _ = await d.claim(key)
    assert verdict == "owner"

    # Owner errored before completing → release so a retry can be processed.
    await d.release_if_pending(key)
    verdict2, cached2 = await d.claim(key)
    assert verdict2 == "owner"
    assert cached2 is None


@pytest.mark.asyncio
async def test_release_is_noop_after_complete() -> None:
    d = MessageDedup()
    key = ("sess", "id-1")
    await d.claim(key)
    await d.complete(key, {"message": "done"})
    # release after completion must not wipe the cached result.
    await d.release_if_pending(key)
    verdict, cached = await d.claim(key)
    assert verdict == "duplicate"
    assert cached == {"message": "done"}


@pytest.mark.asyncio
async def test_in_flight_duplicate_waits_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shorten the wait window so the test is fast.
    monkeypatch.setattr(dedup_mod, "_DUP_WAIT_SECONDS", 2.0)
    d = MessageDedup()
    key = ("sess", "id-1")

    verdict, _ = await d.claim(key)
    assert verdict == "owner"

    async def _finish_later() -> None:
        await asyncio.sleep(0.1)
        await d.complete(key, {"message": "late"})

    finisher = asyncio.create_task(_finish_later())
    # A concurrent retry should block until the owner completes, then get its result.
    verdict2, cached2 = await d.claim(key)
    await finisher
    assert verdict2 == "duplicate"
    assert cached2 == {"message": "late"}


@pytest.mark.asyncio
async def test_no_message_id_keys_are_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sanity: the helper itself doesn't special-case empty ids — that's the
    # caller's job (it only builds a key when both session+id are present). Here
    # we just confirm two empty-id claims with the same key collide as expected.
    # Shorten the wait window so the second (never-completing) claim returns fast.
    monkeypatch.setattr(dedup_mod, "_DUP_WAIT_SECONDS", 0.2)
    d = MessageDedup()
    v1, _ = await d.claim(("sess", ""))
    v2, _ = await d.claim(("sess", ""))
    assert v1 == "owner"
    assert v2 == "duplicate"


@pytest.mark.asyncio
async def test_slow_in_flight_owner_is_not_reprocessed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A slow-but-alive turn (in-flight, younger than the takeover threshold) must
    # NOT be taken over by a same-id retry — the retry stays a "duplicate" so the
    # server never double-runs the LLM. This is the local-slowness regression:
    # the answer-timeout HTTP recovery used to reprocess a still-running turn.
    monkeypatch.setattr(dedup_mod, "_DUP_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(dedup_mod, "_TAKEOVER_SECONDS", 150.0)
    d = MessageDedup()
    key = ("sess", "id-1")

    v1, _ = await d.claim(key)
    assert v1 == "owner"  # the live turn

    # A retry arrives while the owner is still working (well under takeover age).
    # It must NOT become a second owner (which would reprocess) — it's a duplicate
    # whose result is not ready yet.
    v2, cached2 = await d.claim(key)
    assert v2 == "duplicate"
    assert cached2 is None


@pytest.mark.asyncio
async def test_takeover_only_after_owner_presumed_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    # Once an in-flight record is older than the takeover threshold (owner is
    # presumed dead — it would normally release on error), a retry IS allowed to
    # take over and reprocess so the guest still gets an answer.
    monkeypatch.setattr(dedup_mod, "_DUP_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(dedup_mod, "_TAKEOVER_SECONDS", 0.0)
    d = MessageDedup()
    key = ("sess", "id-1")

    v1, _ = await d.claim(key)
    assert v1 == "owner"
    # Threshold is 0 → the existing in-flight record is immediately "too old".
    v2, _ = await d.claim(key)
    assert v2 == "owner"
