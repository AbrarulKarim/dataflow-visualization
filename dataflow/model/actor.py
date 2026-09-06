"""Actors: firing rules, power/timing annotation and sleep policies."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum


class ActorState(str, Enum):
    """The state an actor occupies for a whole cycle.

    Power is charged per cycle according to this state, so exactly one state is
    active at any time.
    """

    IDLE = "idle"
    EXECUTING = "executing"
    SHUTDOWN = "shutdown"  # transitioning idle -> sleeping
    SLEEPING = "sleeping"
    WAKEUP = "wakeup"  # transitioning sleeping -> idle


class ActorKind(str, Enum):
    """What an actor's firing rule does with tokens.

    Named for the *rate behaviour*, not for a model of computation: SDF, HSDF and
    CSDF describe whole graphs, and an SDF graph may perfectly well contain an
    actor whose rates happen to be unit. Calling that actor "HSDF" invites the
    confusion this naming avoids.
    """

    STATIC_RATE = "static_rate"
    UNIT_RATE = "unit_rate"
    PHASED_RATE = "phased_rate"
    SOURCE = "source"
    SINK = "sink"

    @property
    def label(self) -> str:
        return KIND_INFO[self].label

    @property
    def group(self) -> str:
        return KIND_INFO[self].group


@dataclass(frozen=True)
class KindInfo:
    label: str
    group: str  # "static" | "dynamic" | "environment"
    summary: str


KIND_INFO: dict[ActorKind, KindInfo] = {
    ActorKind.STATIC_RATE: KindInfo(
        "Static rate", "static", "fixed token counts per firing"),
    ActorKind.UNIT_RATE: KindInfo(
        "Unit rate", "static", "exactly one token per port"),
    ActorKind.PHASED_RATE: KindInfo(
        "Phased rate", "static", "a rate pattern cycling through phases"),
    ActorKind.SOURCE: KindInfo(
        "Source", "environment", "generates tokens on an interval"),
    ActorKind.SINK: KindInfo(
        "Sink", "environment", "consumes tokens and measures throughput"),
}

#: Names used before the rate-based rename; still accepted when loading a file.
LEGACY_KINDS = {
    "sdf": ActorKind.STATIC_RATE,
    "hsdf": ActorKind.UNIT_RATE,
    "csdf": ActorKind.PHASED_RATE,
}


def parse_kind(value: str | ActorKind) -> ActorKind:
    """Accept the current names and the pre-rename ones alike."""
    if isinstance(value, ActorKind):
        return value
    return LEGACY_KINDS.get(value, None) or ActorKind(value)


#: Actors that are infrastructure rather than part of the computation. They never
#: sleep and are excluded from the global power/energy figures.
SPECIAL_KINDS = frozenset({ActorKind.SOURCE, ActorKind.SINK})


@dataclass
class PowerModel:
    """Power drawn in each state, in arbitrary but consistent units."""

    exec_power: float = 1.0
    idle_power: float = 0.3
    sleep_power: float = 0.02
    shutdown_power: float = 0.5
    wakeup_power: float = 0.8

    def of(self, state: ActorState) -> float:
        return {
            ActorState.EXECUTING: self.exec_power,
            ActorState.IDLE: self.idle_power,
            ActorState.SLEEPING: self.sleep_power,
            ActorState.SHUTDOWN: self.shutdown_power,
            ActorState.WAKEUP: self.wakeup_power,
        }[state]


@dataclass
class TimingModel:
    """Durations in cycles. Transitions of length 0 complete instantaneously."""

    exec_time: int = 1
    sleep_delay: int = 2
    wakeup_delay: int = 3

    def __post_init__(self) -> None:
        if self.exec_time < 1:
            raise ValueError("exec_time must be >= 1 cycle")
        if self.sleep_delay < 0 or self.wakeup_delay < 0:
            raise ValueError("sleep/wakeup delays must be >= 0")


# --------------------------------------------------------------------------- #
# Sleep policies
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdaptiveStrategyInfo:
    label: str
    summary: str


#: Sub-choices for ``SleepPolicy.kind == "adaptive"``. Each strategy computes
#: its own timeout from an actor's fireability history; more strategies land
#: here over time, alongside whatever parameters they need on ``SleepPolicy``.
ADAPTIVE_STRATEGY_INFO: dict[str, AdaptiveStrategyInfo] = {
    "weighted_moving_average": AdaptiveStrategyInfo(
        "Weighted moving average",
        "delay = X × execution time / (average of the last N fireability gaps)",
    ),
}


@dataclass
class SleepPolicy:
    """Decides when a non-fireable idle actor starts shutting down.

    ``idle_cycles`` counts how many *whole* cycles the actor has already spent
    idle-and-not-fireable; it is 0 on the very cycle it becomes non-fireable, and
    is reset whenever the actor becomes fireable again. Once shutdown begins the
    decision is irrevocable -- see ``sim.core`` for the non-interruptible rule.

    ``adaptive_strategy``, ``wma_factor`` and ``wma_window`` are only meaningful
    when ``kind == "adaptive"``; the other kinds ignore them. Kept flat rather
    than nested (matching :class:`Distribution` below) so the JSON schema and
    the builder API stay simple -- a second strategy would add its own
    similarly-prefixed fields rather than a variant type.
    """

    kind: str = "never"
    timeout: int = 0
    adaptive_strategy: str = "weighted_moving_average"
    wma_factor: float = 50.0  # X
    wma_window: int = 5  # N -- how many gaps the average is taken over

    def __post_init__(self) -> None:
        if self.wma_window < 1:
            raise ValueError("adaptive window (N) must be >= 1")
        if self.wma_factor < 0:
            raise ValueError("adaptive factor (X) must be >= 0")

    def should_sleep(
        self, idle_cycles: int, fireable_gaps: Sequence[int] = (), exec_time: int = 1,
    ) -> bool:
        if self.kind == "never":
            return False
        if self.kind == "immediate":
            return True
        if self.kind in ("timeout", "adaptive"):
            return idle_cycles >= self.effective_timeout(fireable_gaps, exec_time)
        raise ValueError(f"unknown sleep policy {self.kind!r}")

    def effective_timeout(self, fireable_gaps: Sequence[int] = (), exec_time: int = 1) -> float:
        """The idle-cycle threshold ``timeout`` and ``adaptive`` decide against.

        For ``timeout`` this is just the configured value. For ``adaptive`` it is
        recomputed from the actor's own recent history every time it is asked --
        the event engine relies on that to predict the same deadline the tick
        engine would reach by re-evaluating every cycle (see
        ``SimulationCore.next_event_time``).
        """
        if self.kind != "adaptive":
            return self.timeout
        if self.adaptive_strategy == "weighted_moving_average":
            if not fireable_gaps:
                # No history yet: fall back to the configured bootstrap timeout
                # rather than guessing, which is also what the pre-adaptive
                # placeholder did (it just used `timeout` unconditionally).
                return float(self.timeout)
            # wma_window (N) bounds how many gaps are averaged -- see
            # ActorRuntime.fireable_gaps -- but does not itself appear in the
            # formula; wma_factor (X) is the free numerator parameter.
            avg_gap = sum(fireable_gaps) / len(fireable_gaps)
            if avg_gap <= 0:
                return float(self.timeout)
            return (self.wma_factor * exec_time) / avg_gap
        raise ValueError(f"unknown adaptive strategy {self.adaptive_strategy!r}")

    @property
    def sleeps(self) -> bool:
        return self.kind != "never"


NEVER_SLEEP = SleepPolicy(kind="never")


# --------------------------------------------------------------------------- #
# Source inter-arrival distributions
# --------------------------------------------------------------------------- #


@dataclass
class Distribution:
    """Inter-arrival time (in cycles) between successive source firings."""

    kind: str = "constant"
    mean: float = 1.0
    stddev: float = 0.0
    low: float = 1.0
    high: float = 1.0
    expression: str = ""  # python expression over `rng`, used when kind == "custom"

    def sample(self, rng: random.Random) -> int:
        if self.kind == "constant":
            value = self.mean
        elif self.kind == "uniform":
            value = rng.uniform(self.low, self.high)
        elif self.kind == "gaussian":
            value = rng.gauss(self.mean, self.stddev)
        elif self.kind == "exponential":
            value = rng.expovariate(1.0 / self.mean) if self.mean > 0 else 0.0
        elif self.kind == "custom":
            value = float(eval(self.expression, {"__builtins__": {}}, {  # noqa: S307
                "rng": rng, "math": math, "mean": self.mean, "stddev": self.stddev,
            }))
        else:
            raise ValueError(f"unknown distribution {self.kind!r}")
        return max(1, int(round(value)))


# --------------------------------------------------------------------------- #
# Actor
# --------------------------------------------------------------------------- #


@dataclass
class Actor:
    """A dataflow actor.

    Firing semantics (all kinds):

    * An implicit self-loop holding a single token is taken when a firing starts
      and returned when it ends, so an actor never has two firings in flight.
    * Input tokens are *checked* at firing start and actually consumed at firing
      end; outputs are produced at firing end.
    * An actor is fireable only if every input holds enough tokens **and** every
      output has room for what this firing will produce (blocked-on-write).
    """

    id: str
    name: str = ""
    kind: ActorKind = ActorKind.STATIC_RATE
    power: PowerModel = field(default_factory=PowerModel)
    timing: TimingModel = field(default_factory=TimingModel)
    sleep_policy: SleepPolicy = field(default_factory=SleepPolicy)
    phases: int = 1  # phase count for phased-rate actors; 1 otherwise
    distribution: Distribution | None = None  # source only
    position: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id
        if self.kind in SPECIAL_KINDS:
            # Sources and sinks are infrastructure: they never sleep.
            self.sleep_policy = NEVER_SLEEP
        if self.kind is ActorKind.SOURCE and self.distribution is None:
            self.distribution = Distribution()
        if self.kind is not ActorKind.PHASED_RATE:
            self.phases = 1

    @property
    def is_special(self) -> bool:
        return self.kind in SPECIAL_KINDS

    @property
    def can_sleep(self) -> bool:
        return not self.is_special and self.sleep_policy.sleeps
