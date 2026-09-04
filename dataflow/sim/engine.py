"""Simulator facade: the tick reference engine and the discrete-event engine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from ..model.graph import DataflowGraph
from . import metrics as metrics_mod
from .core import INF, SimulationCore
from .metrics import RunMetrics
from .trace import TraceConfig, TraceRecorder

EngineMode = Literal["tick", "event"]


def _prime(core: SimulationCore, recorder: TraceRecorder) -> None:
    """Snapshot the network as the trace window opens.

    Called just before the first boundary at or after ``config.start``: nothing
    has changed since the previous boundary, so the states in hand are exactly
    the ones held across cycle ``config.start``.
    """
    if not recorder.primed:
        recorder.prime(core.actor_states(), core.channel_tokens())


@dataclass
class RunResult:
    metrics: RunMetrics
    trace: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics.to_dict(), "trace": self.trace}


class Simulator:
    """Runs a graph for a number of cycles under one of the two engines.

    ``tick`` walks every cycle and is the semantic reference; ``event`` jumps
    straight to the next cycle at which something can change. They are required
    to produce identical traces and metrics (see ``tests/test_engines.py``).
    """

    def __init__(
        self,
        graph: DataflowGraph,
        *,
        seed: int = 0,
        trace: TraceConfig | None = None,
    ) -> None:
        self.graph = graph
        self.seed = seed
        self.trace_config = trace if trace is not None else TraceConfig()

    def run(self, cycles: int, mode: EngineMode = "event") -> RunResult:
        if cycles < 0:
            raise ValueError("cycles must be >= 0")
        core = SimulationCore(self.graph, seed=self.seed)
        recorder = TraceRecorder(config=self.trace_config)
        if self.trace_config.enabled:
            core.on_actor_state = recorder.record_state
            core.on_channel_change = recorder.record_tokens
            core.on_fireable = recorder.record_fireable
        else:
            recorder = None  # type: ignore[assignment]

        started = time.perf_counter()
        if mode == "tick":
            self._run_tick(core, cycles, recorder)
        elif mode == "event":
            self._run_event(core, cycles, recorder)
        else:
            raise ValueError(f"unknown engine mode {mode!r}")
        elapsed = time.perf_counter() - started

        if recorder is None:
            recorder = TraceRecorder(config=self.trace_config)
        else:
            _prime(core, recorder)  # a run too short to reach the window
            recorder.finish(cycles)

        result = metrics_mod.collect(core, cycles, mode, wall_time=elapsed)
        return RunResult(metrics=result, trace=recorder.to_dict())

    # -- drivers -------------------------------------------------------------

    @staticmethod
    def _run_tick(core: SimulationCore, cycles: int, rec: TraceRecorder | None) -> None:
        for t in range(cycles):
            if rec is not None and not rec.primed and t >= rec.config.start:
                _prime(core, rec)
            core.boundary(t)
            if core.next_event_time(t) == INF:
                core.deadlocked_at = t
                core.charge(cycles)  # states are frozen; power still accrues
                return
            core.charge(t + 1)

    @staticmethod
    def _run_event(core: SimulationCore, cycles: int, rec: TraceRecorder | None) -> None:
        t = 0
        while t < cycles:
            if rec is not None and not rec.primed and t >= rec.config.start:
                _prime(core, rec)
            core.boundary(t)
            nxt = core.next_event_time(t)
            if nxt == INF:
                core.deadlocked_at = t
                core.charge(cycles)
                return
            core.charge(min(int(nxt), cycles))
            t = int(nxt)


def simulate(
    graph: DataflowGraph,
    cycles: int,
    *,
    mode: EngineMode = "event",
    seed: int = 0,
    trace: TraceConfig | None = None,
) -> RunResult:
    """Convenience wrapper for one-off runs."""
    return Simulator(graph, seed=seed, trace=trace).run(cycles, mode=mode)
