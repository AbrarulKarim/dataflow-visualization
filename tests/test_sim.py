"""Simulation semantics: firing, backpressure, sleep policies, analytics."""

from __future__ import annotations

import pytest

from dataflow.model import Channel, GraphBuilder
from dataflow.sim import TraceConfig, simulate
from helpers import state_at, states_over, tokens_at

MODES = ("tick", "event")


def sleeper_graph(*, sleep="immediate", timeout=0, interval=10):
    """src --> a --> snk, where only `a` has a sleep policy."""
    b = GraphBuilder("sleeper")
    b.source("src", interval=interval, exec_time=1)
    b.actor("a", exec_time=2, sleep=sleep, timeout=timeout,
            sleep_delay=2, wakeup_delay=3)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=4, channel_id="in")
    b.connect("a", "snk", capacity=4, channel_id="out")
    return b.build()


# --------------------------------------------------------------------------- #
# Firing semantics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", MODES)
def test_inputs_are_consumed_only_when_the_firing_completes(mode):
    """Tokens stay visible on the input channel for the whole firing."""
    b = GraphBuilder()
    b.source("src", interval=100, exec_time=1)
    b.actor("a", exec_time=4)
    b.sink("snk")
    b.connect("src", "a", channel_id="in")
    b.connect("a", "snk", channel_id="out")
    trace = simulate(b.build(), 20, mode=mode).trace

    # Source produces at cycle 1; `a` fires during cycles 1..4 and commits at 5.
    assert tokens_at(trace, "in", 1) == 1
    assert states_over(trace, "a", range(1, 5)) == ["executing"] * 4
    assert [tokens_at(trace, "in", c) for c in range(1, 5)] == [1, 1, 1, 1]
    assert tokens_at(trace, "in", 5) == 0
    assert tokens_at(trace, "out", 4) == 0
    assert tokens_at(trace, "out", 5) == 1


@pytest.mark.parametrize("mode", MODES)
def test_self_loop_prevents_overlapping_firings(mode):
    """Even with a backlog of inputs, an actor runs one firing at a time."""
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", exec_time=5)
    b.sink("snk")
    b.connect("src", "a", capacity=100)
    b.connect("a", "snk", capacity=100)
    result = simulate(b.build(), 100, mode=mode)
    a = next(m for m in result.metrics.actors if m.id == "a")
    # `a` waits one cycle for the first token, then never stops: 99 execution
    # cycles back to back, i.e. 19 completed firings and one still in flight.
    assert a.state_cycles["executing"] == 99
    assert a.state_cycles["idle"] == 1
    assert a.firings == 19


@pytest.mark.parametrize("mode", MODES)
def test_backpressure_blocks_a_firing_that_could_not_write(mode):
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", exec_time=1)
    b.sink("snk", exec_time=10)  # slow sink
    b.connect("src", "a", capacity=100)
    b.connect("a", "snk", capacity=2, channel_id="out")
    result = simulate(b.build(), 200, mode=mode)
    # The output channel must never exceed its capacity ...
    assert max(t for _, t in result.trace["channels"]["out"]) <= 2
    # ... and `a` is throttled to the sink's rate, not the source's.
    a = next(m for m in result.metrics.actors if m.id == "a")
    assert a.firings == pytest.approx(20, abs=2)


@pytest.mark.parametrize("mode", MODES)
def test_initial_tokens_bootstrap_a_cycle(mode):
    b = GraphBuilder()
    b.actor("a", exec_time=2)
    b.actor("bb", exec_time=3)
    b.connect("a", "bb", channel_id="fwd")
    b.connect("bb", "a", channel_id="back", initial_tokens=1)
    result = simulate(b.build(), 1000, mode=mode)
    metrics = {m.id: m for m in result.metrics.actors}
    # Self-timed HSDF cycle: the period is the sum of the execution times, so in
    # 1000 cycles each actor executes for its share and fires 1000/5 times --
    # `bb` starts two cycles later, so its 200th firing lands just outside.
    assert metrics["a"].state_cycles["executing"] == 400
    assert metrics["bb"].state_cycles["executing"] == 600
    assert metrics["a"].firings == 200
    assert metrics["bb"].firings == 199


# --------------------------------------------------------------------------- #
# Sleep state machine
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", MODES)
def test_shutdown_is_not_interruptible(mode):
    """A token arriving mid-shutdown costs the full sleep+wakeup round trip."""
    trace = simulate(sleeper_graph(), 30, mode=mode).trace

    # Not fireable at cycle 0 -> shutdown starts immediately (sleep_delay 2).
    assert states_over(trace, "a", range(0, 2)) == ["shutdown", "shutdown"]
    # The token lands at cycle 1, but shutdown runs to completion regardless.
    assert tokens_at(trace, "in", 1) == 1
    # Fully asleep at 2, so wakeup (3 cycles) starts there; no idle in between.
    assert states_over(trace, "a", range(2, 5)) == ["wakeup"] * 3
    assert states_over(trace, "a", range(5, 7)) == ["executing"] * 2
    assert state_at(trace, "a", 7) == "shutdown"


@pytest.mark.parametrize("mode", MODES)
def test_fireable_marks_when_work_arrives_not_when_it_starts(mode):
    """The gap between the two is exactly what the sleep policy costs."""
    trace = simulate(sleeper_graph(), 30, mode=mode).trace

    # The source emits at 0 and its token lands at cycle 1, which is when `a`
    # has work -- but it is mid-shutdown, so it only runs at cycle 5.
    assert trace["fireable"]["a"][0] == 1
    assert state_at(trace, "a", 1) == "shutdown"
    assert state_at(trace, "a", 5) == "executing"
    # A mid-transition actor still holds its self-loop token, so it stays
    # fireable across cycles 1..4 -- one mark, not four -- and the next arrives
    # with the next token.
    assert trace["fireable"]["a"][:2] == [1, 11]


@pytest.mark.parametrize("mode", MODES)
def test_a_back_to_back_actor_is_marked_at_every_firing(mode):
    """The self-loop token is returned at each completion, so each firing is a
    fresh rising edge -- not one mark for the whole run of firings."""
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("busy", exec_time=5)
    b.sink("snk", exec_time=1)
    b.connect("src", "busy", capacity=100)
    b.connect("busy", "snk", capacity=100)
    result = simulate(b.build(), 40, mode=mode)

    busy = next(m for m in result.metrics.actors if m.id == "busy")
    marks = result.trace["fireable"]["busy"]
    # It runs without a gap from cycle 1 onwards, five cycles per firing.
    assert busy.state_cycles["executing"] == 39
    assert marks == [1, 6, 11, 16, 21, 26, 31, 36]
    # One mark per firing start: the completed ones plus the one still running.
    assert len(marks) == busy.firings + 1
    # Every mark is the cycle a firing began.
    assert all(state_at(result.trace, "busy", cycle) == "executing" for cycle in marks)


@pytest.mark.parametrize("mode", MODES)
def test_an_executing_actor_is_not_fireable(mode):
    """Work waiting behind an in-flight firing is not a fireable moment."""
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("slow", exec_time=8)
    b.sink("snk", exec_time=1)
    b.connect("src", "slow", capacity=100)
    b.connect("slow", "snk", capacity=100)
    trace = simulate(b.build(), 30, mode=mode).trace
    marks = set(trace["fireable"]["slow"])
    # Inputs are always backed up, yet only the firing boundaries are marked.
    assert marks == {1, 9, 17, 25}
    for cycle in range(2, 9):  # mid-firing cycles are never marked
        assert cycle not in marks


@pytest.mark.parametrize("mode", MODES)
def test_a_source_becomes_fireable_on_its_interval(mode):
    trace = simulate(sleeper_graph(interval=10), 55, mode=mode).trace
    assert trace["fireable"]["src"] == [0, 10, 20, 30, 40, 50]


@pytest.mark.parametrize("mode", MODES)
def test_fireable_edges_are_rising_only(mode):
    """Never two marks without the actor going non-fireable in between."""
    graph = sleeper_graph(sleep="never", interval=3)
    result = simulate(graph, 400, mode=mode)
    for actor_id, cycles in result.trace["fireable"].items():
        assert cycles == sorted(set(cycles)), f"{actor_id} has repeats"
        # A mark can never land while the actor is already executing on the work
        # it marks: the firing that consumed the previous tokens must have ended.
        assert all(0 <= c < 400 for c in cycles)


@pytest.mark.parametrize("mode", MODES)
def test_backpressure_delays_the_fireable_mark(mode):
    """An actor with tokens but no room downstream is not yet fireable."""
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", exec_time=1)
    b.sink("snk", exec_time=20)  # drains very slowly
    b.connect("src", "a", capacity=50)
    b.connect("a", "snk", capacity=1)
    trace = simulate(b.build(), 200, mode=mode).trace
    # `a` always has input waiting, so every mark is the sink making room. The
    # loop is 21 cycles, not 20: the sink frees the slot when its firing ends,
    # then `a`'s own one-cycle firing has to land the next token before the sink
    # can start again.
    marks = trace["fireable"]["a"]
    gaps = {b_ - a_ for a_, b_ in zip(marks, marks[1:])}
    assert gaps == {21}, marks


def test_fireable_marks_respect_the_trace_window():
    windowed = simulate(sleeper_graph(interval=10), 300,
                        trace=TraceConfig(start=100, end=150)).trace
    assert all(100 <= c <= 150 for c in windowed["fireable"]["src"])
    assert windowed["fireable"]["src"] == [100, 110, 120, 130, 140, 150]


@pytest.mark.parametrize("mode", MODES)
def test_never_policy_stays_idle(mode):
    trace = simulate(sleeper_graph(sleep="never"), 30, mode=mode).trace
    states = set(states_over(trace, "a", range(0, 30)))
    assert states == {"idle", "executing"}


@pytest.mark.parametrize("mode", MODES)
def test_timeout_countdown_delays_the_shutdown(mode):
    trace = simulate(sleeper_graph(sleep="timeout", timeout=4), 30, mode=mode).trace
    # The firing ends at cycle 3, so the countdown runs over cycles 3..6 and the
    # shutdown starts on cycle 7 -- four idle cycles later than `immediate`.
    assert states_over(trace, "a", range(1, 3)) == ["executing"] * 2
    assert states_over(trace, "a", range(3, 7)) == ["idle"] * 4
    assert states_over(trace, "a", range(7, 9)) == ["shutdown"] * 2
    assert state_at(trace, "a", 9) == "sleeping"


@pytest.mark.parametrize("mode", MODES)
def test_timeout_countdown_resets_when_work_arrives(mode):
    """A steady trickle of work keeps a long-timeout actor from ever sleeping."""
    graph = sleeper_graph(sleep="timeout", timeout=5, interval=3)
    result = simulate(graph, 300, mode=mode)
    a = next(m for m in result.metrics.actors if m.id == "a")
    assert a.state_cycles["sleeping"] == 0
    assert a.state_cycles["shutdown"] == 0
    assert a.firings > 50


@pytest.mark.parametrize("mode", MODES)
def test_immediate_policy_sleeps_more_than_a_timeout_policy(mode):
    eager = simulate(sleeper_graph(sleep="immediate"), 400, mode=mode).metrics
    lazy = simulate(sleeper_graph(sleep="timeout", timeout=6), 400, mode=mode).metrics
    eager_a = next(m for m in eager.actors if m.id == "a")
    lazy_a = next(m for m in lazy.actors if m.id == "a")
    assert eager_a.state_cycles["sleeping"] > lazy_a.state_cycles["sleeping"]


@pytest.mark.parametrize("mode", MODES)
def test_zero_length_transitions_chain_within_one_boundary(mode):
    b = GraphBuilder()
    b.source("src", interval=6, exec_time=1)
    b.actor("a", exec_time=1, sleep="immediate", sleep_delay=0, wakeup_delay=0)
    b.sink("snk")
    b.connect("src", "a", channel_id="in")
    b.connect("a", "snk")
    trace = simulate(b.build(), 20, mode=mode).trace
    assert state_at(trace, "a", 0) == "sleeping"
    # Token arrives at cycle 1: wake and fire in the same boundary.
    assert state_at(trace, "a", 1) == "executing"


# --------------------------------------------------------------------------- #
# Energy and throughput
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", MODES)
def test_energy_matches_hand_computed_state_occupancy(mode):
    b = GraphBuilder()
    b.actor("a", exec_time=2, exec_power=2.0, idle_power=0.5)
    b.actor("bb", exec_time=3, exec_power=1.0, idle_power=0.25)
    b.connect("a", "bb")
    b.connect("bb", "a", initial_tokens=1)
    result = simulate(b.build(), 1000, mode=mode)
    metrics = {m.id: m for m in result.metrics.actors}

    # Period 5: `a` executes 2 of every 5 cycles, `bb` 3 of every 5.
    assert metrics["a"].state_cycles["executing"] == 400
    assert metrics["a"].state_cycles["idle"] == 600
    assert metrics["a"].energy == pytest.approx(400 * 2.0 + 600 * 0.5)
    assert metrics["bb"].energy == pytest.approx(600 * 1.0 + 400 * 0.25)
    assert result.metrics.energy == pytest.approx(
        metrics["a"].energy + metrics["bb"].energy
    )
    assert result.metrics.avg_power == pytest.approx(result.metrics.energy / 1000)


@pytest.mark.parametrize("mode", MODES)
def test_source_and_sink_energy_is_reported_separately(mode):
    result = simulate(sleeper_graph(), 200, mode=mode)
    metrics = {m.id: m for m in result.metrics.actors}
    assert result.metrics.energy == pytest.approx(metrics["a"].energy)
    assert result.metrics.special_energy == pytest.approx(
        metrics["src"].energy + metrics["snk"].energy
    )


@pytest.mark.parametrize("mode", MODES)
def test_throughput_tracks_the_source_rate(mode):
    graph = sleeper_graph(sleep="never", interval=8)
    result = simulate(graph, 8000, mode=mode)
    assert result.metrics.throughput == pytest.approx(1 / 8, rel=0.02)
    assert result.metrics.per_sink_throughput["snk"] == result.metrics.throughput


@pytest.mark.parametrize("mode", MODES)
def test_sleep_saves_energy_when_the_source_is_slow(mode):
    """The motivating experiment: does sleeping actually pay off here?"""
    awake = simulate(sleeper_graph(sleep="never", interval=40), 4000, mode=mode)
    asleep = simulate(sleeper_graph(sleep="immediate", interval=40), 4000, mode=mode)
    assert asleep.metrics.energy < awake.metrics.energy
    assert asleep.metrics.throughput == pytest.approx(awake.metrics.throughput,
                                                      rel=0.05)


# --------------------------------------------------------------------------- #
# CSDF
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", MODES)
def test_csdf_phases_alternate_production(mode):
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", kind="phased_rate", phases=2, exec_time=1)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=50)
    b.connect("a", "snk", produce=[2, 0], capacity=50, channel_id="out")
    result = simulate(b.build(), 100, mode=mode)
    a = next(m for m in result.metrics.actors if m.id == "a")
    # Two firings produce two tokens, so the average rate is 1 token per firing.
    assert a.tokens_out == a.firings


@pytest.mark.parametrize("mode", MODES)
def test_csdf_consumption_pattern_is_respected(mode):
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", kind="phased_rate", phases=2, exec_time=1)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", consume=[1, 3], capacity=50)
    b.connect("a", "snk", capacity=50)
    result = simulate(b.build(), 200, mode=mode)
    a = next(m for m in result.metrics.actors if m.id == "a")
    assert a.tokens_in == pytest.approx(2 * a.firings, abs=3)


# --------------------------------------------------------------------------- #
# Deadlock and edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", MODES)
def test_cycle_without_initial_tokens_deadlocks_at_cycle_zero(mode):
    b = GraphBuilder()
    b.actor("a")
    b.actor("bb")
    b.connect("a", "bb")
    b.connect("bb", "a")
    result = simulate(b.build(), 500, mode=mode)
    assert result.metrics.deadlock_cycle == 0
    assert result.metrics.cycles == 500  # idle power still accrues


@pytest.mark.parametrize("mode", MODES)
def test_full_buffer_with_no_consumer_progress_deadlocks(mode):
    b = GraphBuilder()
    b.source("src", interval=1, exec_time=1)
    b.actor("a", exec_time=1)
    b.sink("snk")
    b.connect("src", "a", capacity=3)
    # `a` cannot write: its output feeds a starved cycle, so everything jams.
    b.connect("a", "snk", capacity=1)
    graph = b.build()
    graph.add_channel(Channel(id="jam", src="a", dst="a", capacity=1))
    result = simulate(graph, 100, mode=mode)
    assert result.metrics.deadlock_cycle is not None


@pytest.mark.parametrize("mode", MODES)
def test_zero_cycle_run_is_empty(mode):
    result = simulate(sleeper_graph(), 0, mode=mode)
    assert result.metrics.cycles == 0
    assert result.metrics.throughput == 0.0
    assert result.metrics.energy == 0.0


def test_trace_window_limits_recording_without_affecting_metrics():
    graph = sleeper_graph(interval=5)
    full = simulate(graph, 2000)
    windowed = simulate(graph, 2000, trace=TraceConfig(start=100, end=200))
    assert windowed.metrics.energy == pytest.approx(full.metrics.energy)
    assert windowed.trace["events"] < full.trace["events"]
    assert all(at >= 100 for at, _ in windowed.trace["actors"]["a"][1:])
    assert all(at <= 200 for at, _ in windowed.trace["actors"]["a"])


@pytest.mark.parametrize("mode", MODES)
def test_windowed_trace_opens_with_the_real_state_not_a_fresh_one(mode):
    """The window's first sample must be what the actor was actually doing."""
    graph = sleeper_graph(sleep="immediate", interval=40)
    full = simulate(graph, 600, mode=mode).trace
    for start in (7, 41, 100, 233):
        windowed = simulate(graph, 600, mode=mode,
                            trace=TraceConfig(start=start)).trace
        assert windowed["first_cycle"] == start
        for entity in ("a", "src", "snk"):
            assert state_at(windowed, entity, start) == state_at(full, entity, start)
        assert tokens_at(windowed, "in", start) == tokens_at(full, "in", start)


@pytest.mark.parametrize("mode", MODES)
def test_trace_covers_the_quiet_tail_after_the_last_event(mode):
    """A deadlocked network still has states worth scrubbing and shading."""
    b = GraphBuilder()
    b.actor("a")
    b.actor("bb")
    b.connect("a", "bb")
    b.connect("bb", "a")
    result = simulate(b.build(), 400, mode=mode)
    assert result.metrics.deadlock_cycle == 0
    assert result.trace["last_cycle"] == 399
    assert state_at(result.trace, "a", 399) == "idle"


def test_trace_can_be_disabled_entirely():
    result = simulate(sleeper_graph(), 500, trace=TraceConfig(enabled=False))
    assert result.trace["events"] == 0
    assert result.metrics.energy > 0


def test_max_events_truncates_and_flags():
    result = simulate(sleeper_graph(interval=2), 5000,
                      trace=TraceConfig(max_events=50))
    assert result.trace["truncated"] is True
    assert result.trace["events"] == 50
