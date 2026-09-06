"""Versioned JSON persistence (``.dfg.json``) and a scripting builder API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .actor import (
    Actor,
    ActorKind,
    Distribution,
    PowerModel,
    SleepPolicy,
    TimingModel,
    parse_kind,
)
from .channel import Channel
from .graph import DataflowGraph

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def actor_to_dict(actor: Actor) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": actor.id,
        "name": actor.name,
        "kind": actor.kind.value,
        "phases": actor.phases,
        "position": list(actor.position),
        "power": vars(actor.power).copy(),
        "timing": vars(actor.timing).copy(),
        "sleep_policy": vars(actor.sleep_policy).copy(),
    }
    if actor.distribution is not None:
        data["distribution"] = vars(actor.distribution).copy()
    return data


def actor_from_dict(data: dict[str, Any]) -> Actor:
    dist = data.get("distribution")
    return Actor(
        id=data["id"],
        name=data.get("name", ""),
        kind=parse_kind(data.get("kind", ActorKind.STATIC_RATE)),
        power=PowerModel(**data.get("power", {})),
        timing=TimingModel(**data.get("timing", {})),
        sleep_policy=SleepPolicy(**data.get("sleep_policy", {})),
        phases=data.get("phases", 1),
        distribution=Distribution(**dist) if dist else None,
        position=tuple(data.get("position", (0.0, 0.0))),  # type: ignore[arg-type]
    )


def channel_to_dict(channel: Channel) -> dict[str, Any]:
    return {
        "id": channel.id,
        "name": channel.name,
        "src": channel.src,
        "dst": channel.dst,
        "capacity": channel.capacity,
        "initial_tokens": channel.initial_tokens,
        "production_rate": channel.production_rate,
        "consumption_rate": channel.consumption_rate,
    }


def channel_from_dict(data: dict[str, Any]) -> Channel:
    return Channel(
        id=data["id"],
        src=data["src"],
        dst=data["dst"],
        capacity=data.get("capacity"),
        initial_tokens=data.get("initial_tokens", 0),
        production_rate=data.get("production_rate", 1),
        consumption_rate=data.get("consumption_rate", 1),
        name=data.get("name", ""),
    )


def graph_to_dict(graph: DataflowGraph) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "name": graph.name,
        "actors": [actor_to_dict(a) for a in graph.actors.values()],
        "channels": [channel_to_dict(c) for c in graph.channels.values()],
    }


def graph_from_dict(data: dict[str, Any]) -> DataflowGraph:
    version = data.get("version", SCHEMA_VERSION)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"file uses schema version {version}, this build understands "
            f"up to {SCHEMA_VERSION}"
        )
    graph = DataflowGraph(name=data.get("name", "untitled"))
    for actor_data in data.get("actors", []):
        graph.add_actor(actor_from_dict(actor_data))
    for channel_data in data.get("channels", []):
        graph.add_channel(channel_from_dict(channel_data))
    return graph


def save_graph(graph: DataflowGraph, path: str | Path) -> None:
    Path(path).write_text(json.dumps(graph_to_dict(graph), indent=2) + "\n")


def load_graph(path: str | Path) -> DataflowGraph:
    return graph_from_dict(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- #
# Builder API -- for scripting parameter sweeps without the GUI
# --------------------------------------------------------------------------- #


class GraphBuilder:
    """Small fluent helper around :class:`DataflowGraph`.

    >>> b = GraphBuilder("chain")
    >>> b.source("src", interval=4)
    >>> b.actor("a", exec_time=3, sleep="timeout", timeout=5)
    >>> b.sink("snk")
    >>> b.connect("src", "a", capacity=4)
    >>> b.connect("a", "snk")
    >>> graph = b.build()
    """

    def __init__(self, name: str = "untitled") -> None:
        self.graph = DataflowGraph(name=name)
        self._channel_seq = 0

    def actor(
        self,
        actor_id: str,
        *,
        kind: str | ActorKind = ActorKind.STATIC_RATE,
        name: str = "",
        exec_time: int = 1,
        phases: int = 1,
        sleep: str = "never",
        timeout: int = 0,
        adaptive_strategy: str = "weighted_moving_average",
        wma_factor: float = 50.0,
        wma_window: int = 5,
        custom_expression: str = "",
        position: tuple[float, float] = (0.0, 0.0),
        **params: float,
    ) -> Actor:
        """Add an actor. ``params`` may set any power/timing field by name.

        ``adaptive_strategy``, ``wma_factor``, ``wma_window`` and
        ``custom_expression`` only matter when ``sleep="adaptive"``; see
        :class:`~dataflow.model.actor.SleepPolicy`.
        """
        power_fields = set(vars(PowerModel()))
        timing_fields = set(vars(TimingModel()))
        power = PowerModel(**{k: v for k, v in params.items() if k in power_fields})
        timing = TimingModel(
            exec_time=exec_time,
            **{k: int(v) for k, v in params.items() if k in timing_fields
               and k != "exec_time"},
        )
        unknown = set(params) - power_fields - timing_fields
        if unknown:
            raise TypeError(f"unknown actor parameters: {sorted(unknown)}")
        return self.graph.add_actor(
            Actor(
                id=actor_id,
                name=name or actor_id,
                kind=parse_kind(kind),
                power=power,
                timing=timing,
                sleep_policy=SleepPolicy(
                    kind=sleep, timeout=timeout, adaptive_strategy=adaptive_strategy,
                    wma_factor=wma_factor, wma_window=wma_window,
                    custom_expression=custom_expression,
                ),
                phases=phases,
                position=position,
            )
        )

    def source(
        self,
        actor_id: str,
        *,
        interval: float = 1.0,
        distribution: str = "constant",
        stddev: float = 0.0,
        low: float = 1.0,
        high: float = 1.0,
        expression: str = "",
        **kwargs: Any,
    ) -> Actor:
        actor = self.actor(actor_id, kind=ActorKind.SOURCE, **kwargs)
        actor.distribution = Distribution(
            kind=distribution,
            mean=interval,
            stddev=stddev,
            low=low,
            high=high,
            expression=expression,
        )
        return actor

    def sink(self, actor_id: str, **kwargs: Any) -> Actor:
        return self.actor(actor_id, kind=ActorKind.SINK, **kwargs)

    def connect(
        self,
        src: str,
        dst: str,
        *,
        produce: int | list[int] = 1,
        consume: int | list[int] = 1,
        capacity: int | None = None,
        initial_tokens: int = 0,
        channel_id: str = "",
    ) -> Channel:
        self._channel_seq += 1
        return self.graph.add_channel(
            Channel(
                id=channel_id or f"ch{self._channel_seq}",
                src=src,
                dst=dst,
                production_rate=produce,
                consumption_rate=consume,
                capacity=capacity,
                initial_tokens=initial_tokens,
            )
        )

    def build(self, *, validate: bool = True) -> DataflowGraph:
        if validate:
            self.graph.validate_or_raise()
        return self.graph
