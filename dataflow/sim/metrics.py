"""Throughput, power and energy figures derived from a finished run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.actor import ActorKind, ActorState
from .core import SimulationCore


@dataclass
class ActorMetrics:
    id: str
    name: str
    kind: str
    kind_label: str
    firings: int
    energy: float
    avg_power: float
    state_cycles: dict[str, int]
    tokens_in: int
    tokens_out: int
    utilization: float  # fraction of cycles spent executing

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "firings": self.firings,
            "energy": self.energy,
            "avg_power": self.avg_power,
            "state_cycles": self.state_cycles,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "utilization": self.utilization,
        }


@dataclass
class RunMetrics:
    """Aggregate results.

    ``energy``/``avg_power`` cover the compute actors only: sources and sinks are
    test-bench infrastructure and are reported separately in ``special_energy``.
    """

    cycles: int
    engine: str
    throughput: float  # tokens per cycle, summed over sinks
    tokens_consumed: int
    tokens_produced: int
    energy: float
    avg_power: float
    special_energy: float
    deadlock_cycle: int | None
    actors: list[ActorMetrics] = field(default_factory=list)
    per_sink_throughput: dict[str, float] = field(default_factory=dict)
    wall_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "engine": self.engine,
            "throughput": self.throughput,
            "tokens_consumed": self.tokens_consumed,
            "tokens_produced": self.tokens_produced,
            "energy": self.energy,
            "avg_power": self.avg_power,
            "special_energy": self.special_energy,
            "deadlock_cycle": self.deadlock_cycle,
            "actors": [a.to_dict() for a in self.actors],
            "per_sink_throughput": self.per_sink_throughput,
            "wall_time": self.wall_time,
        }

    def summary(self) -> str:
        lines = [
            f"engine            {self.engine}",
            f"cycles            {self.cycles}",
            f"throughput        {self.throughput:.6f} tokens/cycle",
            f"tokens produced   {self.tokens_produced}",
            f"tokens consumed   {self.tokens_consumed}",
            f"energy (compute)  {self.energy:.3f}",
            f"avg power         {self.avg_power:.6f}",
            f"energy (src/snk)  {self.special_energy:.3f}",
        ]
        if self.deadlock_cycle is not None:
            lines.append(f"DEADLOCK at cycle {self.deadlock_cycle}")
        lines.append("")
        header = f"{'actor':<14}{'kind':<13}{'firings':>9}{'util':>8}{'energy':>12}"
        lines.append(header)
        lines.append("-" * len(header))
        for actor in self.actors:
            lines.append(
                f"{actor.name[:13]:<14}{actor.kind_label[:12]:<13}{actor.firings:>9}"
                f"{actor.utilization:>7.1%}{actor.energy:>12.3f}"
            )
        return "\n".join(lines)


def collect(core: SimulationCore, cycles: int, engine: str, wall_time: float = 0.0)\
        -> RunMetrics:
    actors: list[ActorMetrics] = []
    compute_energy = 0.0
    special_energy = 0.0
    consumed = 0
    produced = 0
    per_sink: dict[str, float] = {}

    for rt in core.order:
        state_cycles = {s.value: rt.state_cycles[s] for s in ActorState}
        # Integrate energy once, in a fixed state order, so the result does not
        # depend on how the engine chunked time.
        energy = sum(rt.actor.power.of(s) * rt.state_cycles[s] for s in ActorState)
        metrics = ActorMetrics(
            id=rt.id,
            name=rt.actor.name,
            kind=rt.actor.kind.value,
            kind_label=rt.actor.kind.label,
            firings=rt.firings,
            energy=energy,
            avg_power=energy / cycles if cycles else 0.0,
            state_cycles=state_cycles,
            tokens_in=rt.tokens_in,
            tokens_out=rt.tokens_out,
            utilization=(rt.state_cycles[ActorState.EXECUTING] / cycles)
            if cycles else 0.0,
        )
        actors.append(metrics)
        if rt.actor.is_special:
            special_energy += energy
        else:
            compute_energy += energy
        if rt.actor.kind is ActorKind.SINK:
            consumed += rt.tokens_in
            per_sink[rt.id] = rt.tokens_in / cycles if cycles else 0.0
        if rt.actor.kind is ActorKind.SOURCE:
            produced += rt.tokens_out

    return RunMetrics(
        cycles=cycles,
        engine=engine,
        throughput=consumed / cycles if cycles else 0.0,
        tokens_consumed=consumed,
        tokens_produced=produced,
        energy=compute_energy,
        avg_power=compute_energy / cycles if cycles else 0.0,
        special_energy=special_energy,
        deadlock_cycle=core.deadlocked_at,
        actors=actors,
        per_sink_throughput=per_sink,
        wall_time=wall_time,
    )
