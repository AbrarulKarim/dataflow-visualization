"""The dataflow graph: actors, channels, topology queries and validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .actor import Actor, ActorKind
from .channel import Channel


class ValidationError(Exception):
    """Raised when a graph cannot be simulated as specified."""


@dataclass
class DataflowGraph:
    name: str = "untitled"
    actors: dict[str, Actor] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)

    # -- construction --------------------------------------------------------

    def add_actor(self, actor: Actor) -> Actor:
        if actor.id in self.actors:
            raise ValidationError(f"duplicate actor id {actor.id!r}")
        self.actors[actor.id] = actor
        return actor

    def add_channel(self, channel: Channel) -> Channel:
        if channel.id in self.channels:
            raise ValidationError(f"duplicate channel id {channel.id!r}")
        for endpoint in (channel.src, channel.dst):
            if endpoint not in self.actors:
                raise ValidationError(
                    f"channel {channel.id!r} references unknown actor {endpoint!r}"
                )
        self.channels[channel.id] = channel
        return channel

    def remove_actor(self, actor_id: str) -> None:
        self.actors.pop(actor_id, None)
        for cid in [c.id for c in self.channels.values()
                    if c.src == actor_id or c.dst == actor_id]:
            del self.channels[cid]

    def remove_channel(self, channel_id: str) -> None:
        self.channels.pop(channel_id, None)

    # -- topology ------------------------------------------------------------

    def inputs_of(self, actor_id: str) -> list[Channel]:
        return [c for c in self.channels.values() if c.dst == actor_id]

    def outputs_of(self, actor_id: str) -> list[Channel]:
        return [c for c in self.channels.values() if c.src == actor_id]

    def actor_order(self) -> list[str]:
        """Stable, deterministic firing order used to break ties within a cycle."""
        return sorted(self.actors)

    def reset(self) -> None:
        for channel in self.channels.values():
            channel.reset()

    # -- validation ----------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of problems; empty means the graph is simulatable."""
        problems: list[str] = []

        for actor in self.actors.values():
            ins, outs = self.inputs_of(actor.id), self.outputs_of(actor.id)

            if actor.kind is ActorKind.SOURCE:
                if ins:
                    problems.append(f"source {actor.name!r} must have no input channels")
                if not outs:
                    problems.append(f"source {actor.name!r} has no output channel")
            elif actor.kind is ActorKind.SINK:
                if outs:
                    problems.append(f"sink {actor.name!r} must have no output channels")
                if not ins:
                    problems.append(f"sink {actor.name!r} has no input channel")
            elif not ins and not outs:
                problems.append(f"actor {actor.name!r} is disconnected")

            if actor.kind is ActorKind.UNIT_RATE:
                for chan in ins + outs:
                    rate = chan.cons_rate() if chan.dst == actor.id else chan.prod_rate()
                    if rate != 1:
                        problems.append(
                            f"{actor.kind.label} actor {actor.name!r}: channel "
                            f"{chan.id!r} has rate {rate}, must be 1"
                        )

            if actor.kind is ActorKind.PHASED_RATE:
                if actor.phases < 1:
                    problems.append(
                        f"{actor.kind.label} actor {actor.name!r} needs >= 1 phase")
                for chan in ins:
                    if chan.cons_phases() not in (1, actor.phases):
                        problems.append(
                            f"{actor.kind.label} actor {actor.name!r}: channel "
                            f"{chan.id!r} consumption pattern has "
                            f"{chan.cons_phases()} phases, expected "
                            f"{actor.phases}"
                        )
                for chan in outs:
                    if chan.prod_phases() not in (1, actor.phases):
                        problems.append(
                            f"{actor.kind.label} actor {actor.name!r}: channel "
                            f"{chan.id!r} production pattern has "
                            f"{chan.prod_phases()} phases, expected "
                            f"{actor.phases}"
                        )
            else:
                for chan in ins:
                    if chan.cons_phases() != 1:
                        problems.append(
                            f"actor {actor.name!r} is {actor.kind.label} but channel "
                            f"{chan.id!r} has a multi-phase consumption pattern"
                        )
                for chan in outs:
                    if chan.prod_phases() != 1:
                        problems.append(
                            f"actor {actor.name!r} is {actor.kind.label} but channel "
                            f"{chan.id!r} has a multi-phase production pattern"
                        )

        for chan in self.channels.values():
            rates = [chan.prod_rate(p) for p in range(chan.prod_phases())]
            rates += [chan.cons_rate(p) for p in range(chan.cons_phases())]
            if any(r < 0 for r in rates):
                problems.append(f"channel {chan.id!r} has a negative rate")
            if chan.capacity is not None:
                if chan.capacity < 1:
                    problems.append(f"channel {chan.id!r} has capacity < 1")
                elif chan.initial_tokens > chan.capacity:
                    problems.append(
                        f"channel {chan.id!r} starts with {chan.initial_tokens} tokens "
                        f"but has capacity {chan.capacity}"
                    )
                else:
                    worst = max(chan.prod_rate(p) for p in range(chan.prod_phases()))
                    if worst > chan.capacity:
                        problems.append(
                            f"channel {chan.id!r} capacity {chan.capacity} is smaller "
                            f"than a single production of {worst} tokens: its producer "
                            f"can never fire"
                        )
            if chan.initial_tokens < 0:
                problems.append(f"channel {chan.id!r} has negative initial tokens")

        return problems

    def validate_or_raise(self) -> None:
        problems = self.validate()
        if problems:
            raise ValidationError("; ".join(problems))
