"""The bundled networks must stay valid, live and consistent.

A rate-inconsistent dataflow graph either deadlocks against its bounded channels
or grows a buffer without limit, and both are easy to introduce by editing a
rate. These run every example far enough to reach steady state and check it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dataflow.model import load_graph
from dataflow.sim import Simulator, TraceConfig, simulate

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.dfg.json"))
NAMES = [path.name.removesuffix(".dfg.json") for path in EXAMPLE_FILES]

#: Long enough for every example's slowest source to fire many times.
CYCLES = 200_000


@pytest.fixture(scope="module", params=EXAMPLE_FILES, ids=NAMES)
def example(request):
    return load_graph(request.param)


def test_there_are_examples_to_check():
    assert len(EXAMPLE_FILES) >= 11, "expected the bundled example networks"


def test_example_validates(example):
    assert example.validate() == []


def test_example_runs_without_deadlocking(example):
    result = simulate(example, CYCLES, trace=TraceConfig(enabled=False))
    assert result.metrics.deadlock_cycle is None
    assert result.metrics.throughput > 0, "no tokens reached the sink"
    assert all(a.firings > 0 for a in result.metrics.actors), "an actor never fired"


def test_example_reaches_a_bounded_steady_state(example):
    """Rate-consistent graphs settle: doubling the run doubles the work done.

    An inconsistent one either stalls or piles tokens up in a channel, and the
    ratio drifts away from 2 in one direction or the other.
    """
    short = simulate(example, CYCLES, trace=TraceConfig(enabled=False)).metrics
    long = simulate(example, CYCLES * 2, trace=TraceConfig(enabled=False)).metrics
    assert long.tokens_consumed == pytest.approx(2 * short.tokens_consumed, rel=0.05)


def test_example_bounded_channels_never_overflow(example):
    """Backpressure has to hold every bounded FIFO inside its capacity."""
    result = simulate(example, 20_000)
    for channel_id, series in result.trace["channels"].items():
        capacity = example.channels[channel_id].capacity
        if capacity is None:
            continue
        assert max(tokens for _, tokens in series) <= capacity, channel_id


def test_example_agrees_across_engines(example):
    sim = Simulator(example, trace=TraceConfig(enabled=False))
    tick = sim.run(20_000, mode="tick").metrics
    event = sim.run(20_000, mode="event").metrics
    assert tick.energy == pytest.approx(event.energy)
    assert tick.tokens_consumed == event.tokens_consumed


def test_sleep_policies_are_exercised_somewhere():
    """The sensor node is the sleep-policy showcase: it must actually sleep."""
    graph = load_graph(EXAMPLES_DIR / "sensor-node.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    radio = next(a for a in result.metrics.actors if a.id == "radio_tx")
    assert radio.state_cycles["sleeping"] > radio.state_cycles["idle"]
    assert radio.state_cycles["wakeup"] > 0


def test_sleeping_beats_idling_for_the_sensor_node():
    """The trade-off the example exists to pose, in one assertion."""
    sleepy = load_graph(EXAMPLES_DIR / "sensor-node.dfg.json")
    awake = load_graph(EXAMPLES_DIR / "sensor-node.dfg.json")
    for actor in awake.actors.values():
        actor.sleep_policy.kind = "never"

    sleepy_energy = simulate(sleepy, CYCLES, trace=TraceConfig(enabled=False)).metrics
    awake_energy = simulate(awake, CYCLES, trace=TraceConfig(enabled=False)).metrics
    assert sleepy_energy.energy < awake_energy.energy
    # ... and it costs nothing in throughput at this event rate.
    assert sleepy_energy.tokens_consumed == awake_energy.tokens_consumed


def test_phased_rate_corner_turn_batches_four_pulses():
    """The radar corner turn emits one block per four pulses, by construction."""
    graph = load_graph(EXAMPLES_DIR / "radar-doppler.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    assert firings["corner_turn"] == pytest.approx(4 * firings["doppler_fft"], abs=4)


def test_turbo_decoder_runs_eight_iterations_per_block():
    """The iteration count lives in the rates: eight half-iterations per block."""
    graph = load_graph(EXAMPLES_DIR / "turbo-decoder.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    assert firings["siso_a"] == pytest.approx(8 * firings["crc_check"], abs=8)
    assert firings["siso_b"] == pytest.approx(8 * firings["crc_check"], abs=8)


def test_hevc_encoder_closes_both_of_its_loops():
    """Sixteen CTUs per frame down the fork-join, and both loops turning."""
    graph = load_graph(EXAMPLES_DIR / "hevc-encoder.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    # Fork-join: both candidate branches run once per CTU.
    assert firings["intra_pred"] == pytest.approx(firings["motion_est"], abs=2)
    assert firings["mode_decide"] == pytest.approx(16 * firings["entropy"], abs=16)
    # Reconstruction loop and rate-control loop both turn once per frame.
    assert firings["dpb"] == pytest.approx(firings["entropy"], abs=2)
    assert firings["rate_ctrl"] == pytest.approx(firings["entropy"], abs=2)


def test_slam_outer_loop_is_eight_times_slower_than_the_inner_one():
    """Pose-graph optimisation fires once per eight keyframes, not per frame."""
    graph = load_graph(EXAMPLES_DIR / "slam-frontend.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    assert firings["pose_graph"] == pytest.approx(firings["local_map"] / 8, abs=2)
    # The IMU runs eight times per camera frame and is pre-integrated down to one.
    assert firings["imu"] == pytest.approx(8 * firings["camera"], abs=8)
    assert firings["imu_preint"] == pytest.approx(firings["camera"], abs=2)


def test_echo_canceller_joins_two_live_sources():
    graph = load_graph(EXAMPLES_DIR / "echo-canceller.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    assert firings["far_end"] == pytest.approx(firings["near_mic"], abs=2)
    assert firings["subtract"] == pytest.approx(firings["far_end"], abs=2)
    # The adaptation loop turns once per block, in step with the filter.
    assert firings["lms_update"] == pytest.approx(firings["adaptive_filter"], abs=2)


def test_h264_reference_loop_paces_the_decoder():
    """Nine macroblock rows per frame, all the way down the chain."""
    graph = load_graph(EXAMPLES_DIR / "h264-decoder.dfg.json")
    result = simulate(graph, CYCLES, trace=TraceConfig(enabled=False))
    firings = {a.id: a.firings for a in result.metrics.actors}
    assert firings["cabac"] == pytest.approx(9 * firings["deblock"], abs=9)
    assert firings["display"] == pytest.approx(firings["bitstream"], abs=2)
