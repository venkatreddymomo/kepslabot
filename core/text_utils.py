"""Canonical tiny text helpers shared across services.

Home for pure string helpers that had drifted into multiple byte-identical
copies. Only functions with a single, unambiguous implementation live here;
call sites that historically diverged keep their own local variant on purpose.
"""
from __future__ import annotations

from typing import Any


def normalize_identifier(value: Any) -> str:
    """Lowercase, trim, and underscore-join a value into a stable identifier.

    ``"  Front Desk "`` → ``"front_desk"``. Canonical version of the helper
    that was copy-pasted identically across agent_plugin_service,
    llm_orchestration_service, orchestration_policy_service, ticketing_service,
    and the admin routes.
    """
    return str(value or "").strip().lower().replace(" ", "_")
