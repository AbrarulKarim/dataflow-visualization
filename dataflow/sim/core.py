"""Shared simulation core.

Both the reference tick loop and the discrete-event engine drive *this* object;
they differ only in how they choose the next timestamp. That is what makes the
two engines agree by construction rather than by careful duplication.

Timing model
------------
A cycle is an integer. An actor occupies exactly one :class:`ActorState` for the
whole of cycle ``t``, and is charged ``power.of(state)`` for it. All discrete
changes happen at cycle *boundaries*, in :meth:`SimulationCore.boundary`:

1. every timed activity whose deadline is ``t`` completes (firings commit their
   token moves here), then
2. every actor takes its start/sleep/wakeup decision, in a stable id order.

Doing all completions before any decision is what lets a token produced at ``t``
be picked up by a downstream actor in the very same cycle.
"""

from __future__ import annotations

import random
from typing import Iterable

from ..model.actor import Actor, ActorKind, ActorState
from ..model.channel import Channel
from ..model.graph import DataflowGraph

INF = float("inf")

#: States that end on their own at a known deadline.
TIMED_STATES = (ActorState.EXECUTING, ActorState.SHUTDOWN, ActorState.WAKEUP)


class ActorRuntime:
    """Mutable per-actor simulation state and accumulators."""

    __slots__ = (
        "actor", "inputs", "outputs", "state", "until", "idle_since", "phase",
        "firings", "pending_consume", "pending_produce", "next_fire",
        "state_cycles", "tokens_in", "tokens_out", "was_fireable",
    )

    def __init__(self, actor: Actor, inputs: list[Channel], outputs: list[Channel]):
        self.actor = actor
        self.inputs = inputs
        self.outputs = outputs
        self.state = ActorState.IDLE
        self.until = -1
        self.idle_since: int | None = 0
        self.phase = 0
        self.firings = 0
        self.pending_consume: list[tuple[Channel, int]] = []
        self.pending_produce: list[tuple[Channel, int]] = []
        self.next_fire = 0  # sources only
        self.state_cycles: dict[ActorState, int] = {s: 0 for s in ActorState}
        self.tokens_in = 0
        self.tokens_out = 0
        self.was_fireable = False

    @property
    def id(self) -> str:
        return self.actor.id


class SimulationCore:
    """Cycle-boundary semantics for a :class:`DataflowGraph`."""

    def __init__(self, graph: DataflowGraph, seed: int = 0):
        graph.validate_or_raise()
        graph.reset()
        self.graph = graph
        self.rng = random.Random(seed)
        self.now = 0
        self.deadlocked_at: int | None = None
        self.runtimes: dict[str, ActorRuntime] = {
            aid: ActorRuntime(
                graph.actors[aid], graph.inputs_of(aid), graph.outputs_of(aid)
            )
            for aid in graph.actor_order()
        }
        self.order: list[ActorRuntime] = list(self.runtimes.values())
        for rt in self.order:
            if rt.actor.kind is ActorKind.SOURCE:
                rt.next_fire = 0
        #: Set by the driver so the core can report state/token changes.
        self.on_actor_state = None  # type: ignore[assignment]
        self.on_channel_change = None  # type: ignore[assignment]
        self.on_fireable = None  # type: ignore[assignment]

    # -- helpers -------------------------------------------------------------

    def _set_state(self, rt: ActorRuntime, state: ActorState, t: int) -> None:
        if rt.state is state:
            return
        rt.state = state
        if self.on_actor_state is not None:
            self.on_actor_state(t, rt.id, state)

    def _channel_changed(self, channel: Channel, t: int) -> None:
        if self.on_channel_change is not None:
            self.on_channel_change(t, channel.id, channel.tokens)

    def fireable(self, rt: ActorRuntime, t: int) -> bool:
        """Whether a firing could start right now.

        Three conditions, all of them part of the firing rule:

        * the implicit **self-loop token** is available -- an executing actor is
          holding its own, which is what stops firings from overlapping, so it
          becomes fireable again only when that firing completes and gives the
          token back;
        * every input holds enough tokens;
        * every output has room for what the firing will produce
          (blocked-on-write).

        An actor mid-shutdown or mid-wakeup still has its self-loop token, so it
        counts as fireable: work is available, it simply cannot act on it yet.
        """
        if rt.state is ActorState.EXECUTING:
            return False
        kind = rt.actor.kind
        if kind is ActorKind.SOURCE and t < rt.next_fire:
            return False
        phase = rt.phase
        for chan in rt.inputs:
            if chan.tokens < chan.cons_rate(phase):
                return False
        for chan in rt.outputs:
            if not chan.has_space(chan.prod_rate(phase)):
                return False
        return True

    # -- the cycle boundary --------------------------------------------------

    def boundary(self, t: int) -> None:
        """Apply everything that happens at the boundary entering cycle ``t``."""
        for rt in self.order:
            if rt.state in TIMED_STATES and rt.until == t:
                self._complete(rt, t)
        if self.on_fireable is not None:
            self._report_fireable(t)
        for rt in self.order:
            self._decide(rt, t)

    def _report_fireable(self, t: int) -> None:
        """Report actors that have just become fireable.

        Evaluated after the completions have moved tokens but *before* any
        decision is taken, which is what "work is available entering this cycle"
        means: a source that fires here has not yet pushed its next arrival out,
        and an actor about to start has not yet taken its self-loop token.

        Fireability only changes when a firing completes or a source's arrival
        comes due, and both are cycle boundaries, so sampling here catches every
        transition -- in the event engine as much as in the tick loop.
        """
        for rt in self.order:
            now = self.fireable(rt, t)
            if now and not rt.was_fireable:
                self.on_fireable(t, rt.id)
            rt.was_fireable = now

    def _complete(self, rt: ActorRuntime, t: int) -> None:
        if rt.state is ActorState.EXECUTING:
            for chan, n in rt.pending_consume:
                chan.pop(n)
                rt.tokens_in += n
                self._channel_changed(chan, t)
            for chan, n in rt.pending_produce:
                chan.push(n)
                rt.tokens_out += n
                self._channel_changed(chan, t)
            rt.pending_consume = []
            rt.pending_produce = []
            rt.firings += 1
            rt.phase = (rt.phase + 1) % max(1, rt.actor.phases)
            # The self-loop token is returned here, so the actor may start again
            # in this very same boundary.
            self._set_state(rt, ActorState.IDLE, t)
            rt.idle_since = None
        elif rt.state is ActorState.SHUTDOWN:
            self._set_state(rt, ActorState.SLEEPING, t)
        elif rt.state is ActorState.WAKEUP:
            self._set_state(rt, ActorState.IDLE, t)
            rt.idle_since = None

    def _decide(self, rt: ActorRuntime, t: int) -> None:
        # Bounded loop so that zero-length transitions (sleep_delay or
        # wakeup_delay of 0) can chain within a single boundary.
        for _ in range(4):
            if rt.state is ActorState.IDLE:
                if self.fireable(rt, t):
                    self._start_firing(rt, t)
                    return
                if rt.idle_since is None:
                    rt.idle_since = t
                if rt.actor.can_sleep and rt.actor.sleep_policy.should_sleep(
                    t - rt.idle_since
                ):
                    if rt.actor.timing.sleep_delay == 0:
                        self._set_state(rt, ActorState.SLEEPING, t)
                        return
                    self._set_state(rt, ActorState.SHUTDOWN, t)
                    rt.until = t + rt.actor.timing.sleep_delay
                return
            if rt.state is ActorState.SLEEPING:
                # Non-interruptible shutdown means we only ever get here once the
                # actor is fully asleep; waking costs the full wakeup delay.
                if not self.fireable(rt, t):
                    return
                if rt.actor.timing.wakeup_delay == 0:
                    self._set_state(rt, ActorState.IDLE, t)
                    rt.idle_since = None
                    continue  # may fire immediately
                self._set_state(rt, ActorState.WAKEUP, t)
                rt.until = t + rt.actor.timing.wakeup_delay
                return
            return  # EXECUTING / SHUTDOWN / WAKEUP are non-interruptible

    def _start_firing(self, rt: ActorRuntime, t: int) -> None:
        phase = rt.phase
        rt.pending_consume = [
            (c, c.cons_rate(phase)) for c in rt.inputs if c.cons_rate(phase)
        ]
        rt.pending_produce = [
            (c, c.prod_rate(phase)) for c in rt.outputs if c.prod_rate(phase)
        ]
        if rt.actor.kind is ActorKind.SOURCE:
            assert rt.actor.distribution is not None
            rt.next_fire = t + rt.actor.distribution.sample(self.rng)
        rt.idle_since = None
        # Taking the self-loop token makes the actor non-fireable. Decisions run
        # after the fireability report in a boundary, so record that fall here or
        # the next rising edge -- this firing completing -- would be missed.
        rt.was_fireable = False
        self._set_state(rt, ActorState.EXECUTING, t)
        rt.until = t + rt.actor.timing.exec_time

    # -- time ----------------------------------------------------------------

    def charge(self, until: int) -> None:
        """Bank state occupancy for the interval ``[now, until)`` and move the clock.

        Only cycle counts are accumulated; energy is integrated once, at the end
        of the run. That keeps the two engines bit-identical -- they bank the
        same cycles in differently sized chunks, and floating-point addition is
        not associative.
        """
        dt = until - self.now
        if dt <= 0:
            return
        for rt in self.order:
            rt.state_cycles[rt.state] += dt
        self.now = until

    def next_event_time(self, t: int) -> float:
        """Earliest strictly-future cycle at which any state can change.

        Token counts only move when a firing completes, so fireability can only
        change at a completion, a source's next arrival, or a sleep deadline.
        """
        nxt = INF
        for rt in self.order:
            if rt.state in TIMED_STATES:
                if rt.until > t:
                    nxt = min(nxt, rt.until)
            elif rt.state is ActorState.IDLE:
                if rt.actor.kind is ActorKind.SOURCE and rt.next_fire > t:
                    nxt = min(nxt, rt.next_fire)
                elif rt.actor.can_sleep and rt.idle_since is not None:
                    policy = rt.actor.sleep_policy
                    if policy.kind in ("timeout", "adaptive"):
                        deadline = rt.idle_since + policy.timeout
                        if deadline > t:
                            nxt = min(nxt, deadline)
            if nxt == t + 1:
                break  # cannot do better than the next cycle
        return nxt

    def is_stalled(self) -> bool:
        """True when nothing is running and nothing will ever start again."""
        return self.next_event_time(self.now) == INF

    # -- convenience ---------------------------------------------------------

    def channel_tokens(self) -> dict[str, int]:
        return {cid: c.tokens for cid, c in self.graph.channels.items()}

    def actor_states(self) -> dict[str, str]:
        return {rt.id: rt.state.value for rt in self.order}

    def sinks(self) -> Iterable[ActorRuntime]:
        return (rt for rt in self.order if rt.actor.kind is ActorKind.SINK)

    def sources(self) -> Iterable[ActorRuntime]:
        return (rt for rt in self.order if rt.actor.kind is ActorKind.SOURCE)
