"""Compact change-only trace of a run, used to drive the cycle-by-cycle player."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceConfig:
    """What to record. Metrics are always exact; only the *visual* trace is cut.

    A 10M-cycle run cannot be replayed token-by-token in a browser, so record a
    window (and optionally only every ``stride``-th cycle's worth of changes),
    while the metrics keep covering the whole run.
    """

    enabled: bool = True
    start: int = 0
    end: int | None = None  # None == until the run ends
    max_events: int = 400_000

    def covers(self, cycle: int) -> bool:
        if not self.enabled or cycle < self.start:
            return False
        return self.end is None or cycle <= self.end


@dataclass
class TraceRecorder:
    config: TraceConfig = field(default_factory=TraceConfig)
    actor_states: dict[str, list[list[Any]]] = field(default_factory=dict)
    channel_tokens: dict[str, list[list[Any]]] = field(default_factory=dict)
    #: Cycles at which each actor went from not-fireable to fireable.
    fireable: dict[str, list[int]] = field(default_factory=dict)
    events: int = 0
    truncated: bool = False
    primed: bool = False
    first_cycle: int = 0
    last_cycle: int = 0

    def prime(self, states: dict[str, str], tokens: dict[str, int]) -> None:
        """Seed the timeline with the state everything holds as the window opens.

        For a windowed trace this must be called when the run *reaches*
        ``config.start``, not before it begins: otherwise the timeline would
        claim every actor was idle at the start of the window.
        """
        self.primed = True
        self.first_cycle = self.config.start
        self.last_cycle = self.config.start
        for aid, state in states.items():
            self.actor_states[aid] = [[self.config.start, state]]
            self.fireable.setdefault(aid, [])
        for cid, count in tokens.items():
            self.channel_tokens[cid] = [[self.config.start, count]]

    def finish(self, cycles: int) -> None:
        """Extend the timeline to the end of the window.

        Nothing changes after the last recorded event, but the states held over
        that quiet tail are still part of the window -- the player scrubs across
        it and the power charts shade it.
        """
        if not self.config.enabled or self.truncated or cycles <= 0:
            return
        end = cycles - 1
        if self.config.end is not None:
            end = min(self.config.end, end)
        if end >= self.config.start:
            self.last_cycle = max(self.last_cycle, end)

    def _accept(self, cycle: int) -> bool:
        if not self.config.covers(cycle):
            return False
        if self.events >= self.config.max_events:
            self.truncated = True
            return False
        self.events += 1
        self.last_cycle = max(self.last_cycle, cycle)
        return True

    def record_state(self, cycle: int, actor_id: str, state: Any) -> None:
        if not self._accept(cycle):
            return
        series = self.actor_states.setdefault(actor_id, [])
        value = getattr(state, "value", state)
        if series and series[-1][0] == cycle:
            series[-1][1] = value
        else:
            series.append([cycle, value])

    def record_fireable(self, cycle: int, actor_id: str) -> None:
        if not self._accept(cycle):
            return
        self.fireable.setdefault(actor_id, []).append(cycle)

    def record_tokens(self, cycle: int, channel_id: str, tokens: int) -> None:
        if not self._accept(cycle):
            return
        series = self.channel_tokens.setdefault(channel_id, [])
        if series and series[-1][0] == cycle:
            series[-1][1] = tokens
        else:
            series.append([cycle, tokens])

    def to_dict(self) -> dict[str, Any]:
        return {
            "actors": self.actor_states,
            "channels": self.channel_tokens,
            "fireable": self.fireable,
            "first_cycle": self.first_cycle,
            "last_cycle": self.last_cycle,
            "events": self.events,
            "truncated": self.truncated,
        }
