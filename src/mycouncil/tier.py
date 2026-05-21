"""Client-side model picker — fills config['experts'][*].model and
config['chairman']['model'] based on config['tier'].

Mirrors backend/auto_config.py::assign_models_to_roles so the MCP server can
hide concrete model IDs from the agent: the agent sees roles + tier, and we
pick the actual models locally before sending the config to /api/v1/debate.

If the agent supplies explicit models (advanced power-user case), we respect
those and only fill the missing ones.
"""

from __future__ import annotations

import random
from typing import Any


def _pool_for_tier(
    models: list[dict],
    tier: str,
    session_type: int,
    *,
    orchestrator_only: bool = False,
) -> list[dict]:
    """Filter the whitelist by tier + session_type (+ orchestrator if needed).

    Falls back gracefully when the tier has no models for the requested
    session type, then to any-tier orchestrator pool if even that is empty.
    """
    pool = [
        m
        for m in models
        if m.get("tier") == tier
        and session_type in (m.get("session_types") or [1, 2])
        and (not orchestrator_only or m.get("orchestrator"))
    ]
    if pool:
        return pool

    # Tier-specific fallback: widen to any-tier in the same session_type
    pool = [
        m
        for m in models
        if session_type in (m.get("session_types") or [1, 2])
        and (not orchestrator_only or m.get("orchestrator"))
    ]
    return pool


def _weighted_pick(pool: list[dict]) -> dict:
    """Weighted random pick from pool. priority field is the weight."""
    weights = [max(1, int(m.get("priority") or 1)) for m in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def fill_models_by_tier(config: dict[str, Any], models: list[dict]) -> dict[str, Any]:
    """Fill missing expert.model and chairman.model fields based on config['tier'].

    Mutates and returns the config. If a model is already set on an expert or
    chairman, it's preserved (allows callers to override).

    Defaults:
      - tier: 'balanced' if missing
      - session_type: 1 if missing
    """
    tier = config.get("tier") or "balanced"
    session_type = config.get("session_type", 1)
    expert_pool = _pool_for_tier(models, tier, session_type)
    if not expert_pool:
        raise RuntimeError(
            f"No models available for tier={tier!r} session_type={session_type}. "
            "Check that the server's /api/available-models response exposes the "
            "expected tier metadata."
        )

    for expert in config.get("experts", []):
        if not expert.get("model"):
            expert["model"] = _weighted_pick(expert_pool)["name"]

    chairman = config.get("chairman") or {}
    if not chairman.get("model"):
        chairman_pool = _pool_for_tier(
            models, tier, session_type, orchestrator_only=session_type >= 2
        )
        if not chairman_pool:
            raise RuntimeError(
                f"No orchestrator-capable models available for tier={tier!r} "
                f"session_type={session_type}."
            )
        chairman["model"] = _weighted_pick(chairman_pool)["name"]
        config["chairman"] = chairman

    return config
