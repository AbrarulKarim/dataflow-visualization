"""FIFO channels connecting exactly one producer actor to one consumer actor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


def _rate_at(rate: int | Sequence[int], phase: int) -> int:
    """Rate for a given firing phase; scalars are broadcast over all phases."""
    if isinstance(rate, int):
        return rate
    return rate[phase % len(rate)]


@dataclass
class Channel:
    """A bounded or unbounded FIFO.

    Token *values* are not modelled in phase 1 (static dataflow only cares about
    counts), so the FIFO is a counter. When data-dependent actors arrive this
    grows a parallel deque of values; nothing outside this class assumes counts.

    ``production_rate``/``consumption_rate`` are ints for SDF/HSDF and per-phase
    sequences for CSDF actors.
    """

    id: str
    src: str
    dst: str
    capacity: int | None = None  # None == unbounded
    initial_tokens: int = 0
    production_rate: int | list[int] = 1
    consumption_rate: int | list[int] = 1
    name: str = ""

    tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tokens = self.initial_tokens

    # -- rates ---------------------------------------------------------------

    def prod_rate(self, phase: int = 0) -> int:
        return _rate_at(self.production_rate, phase)

    def cons_rate(self, phase: int = 0) -> int:
        return _rate_at(self.consumption_rate, phase)

    def prod_phases(self) -> int:
        return 1 if isinstance(self.production_rate, int) else len(self.production_rate)

    def cons_phases(self) -> int:
        return 1 if isinstance(self.consumption_rate, int) else len(self.consumption_rate)

    # -- FIFO ----------------------------------------------------------------

    @property
    def space(self) -> float:
        return float("inf") if self.capacity is None else self.capacity - self.tokens

    def has_tokens(self, n: int) -> bool:
        return self.tokens >= n

    def has_space(self, n: int) -> bool:
        return self.capacity is None or self.tokens + n <= self.capacity

    def push(self, n: int) -> None:
        if not self.has_space(n):
            raise RuntimeError(
                f"channel {self.id}: overflow pushing {n} into {self.tokens}/{self.capacity}"
            )
        self.tokens += n

    def pop(self, n: int) -> None:
        if not self.has_tokens(n):
            raise RuntimeError(
                f"channel {self.id}: underflow popping {n} from {self.tokens}"
            )
        self.tokens -= n
