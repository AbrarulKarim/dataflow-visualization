"""Shared test utilities."""

from __future__ import annotations

from typing import Any


def series_at(series: list[list[Any]], cycle: int) -> Any:
    """Value of a change-only trace series during ``cycle``."""
    value = None
    for at, val in series:
        if at > cycle:
            break
        value = val
    return value


def state_at(trace: dict[str, Any], actor_id: str, cycle: int) -> str:
    return series_at(trace["actors"][actor_id], cycle)


def tokens_at(trace: dict[str, Any], channel_id: str, cycle: int) -> int:
    return series_at(trace["channels"][channel_id], cycle)


def states_over(trace: dict[str, Any], actor_id: str, cycles: range) -> list[str]:
    return [state_at(trace, actor_id, c) for c in cycles]
