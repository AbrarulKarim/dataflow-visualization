"""The two engines must be indistinguishable, and the fast one must be fast."""

from __future__ import annotations

import random
import time

import pytest

from dataflow.model import GraphBuilder
from dataflow.sim import Simulator, TraceConfig

SLEEP_KINDS = ("never", "immediate", "timeout", "adaptive")
DISTRIBUTIONS = ("constant", "uniform", "gaussian", "exponential")


def random_graph(rng: random.Random) -> object:
    """A random pipeline with occasional feedback edges and bounded buffers."""
    b = GraphBuilder(f"rnd{rng.random()}")
    depth = rng.randint(1, 4)
    b.source(
        "src",
        interval=rng.randint(1, 12),
        distribution=rng.choice(DISTRIBUTIONS),
        stddev=rng.uniform(0.5, 3.0),
        low=1,
        high=rng.randint(2, 10),
        exec_time=rng.randint(1, 3),
    )
    stage_ids = []
    for i in range(depth):
        aid = f"a{i}"
        stage_ids.append(aid)
        b.actor(
            aid,
            kind=rng.choice(("static_rate", "phased_rate"))
            if rng.random() < 0.3 else "static_rate",
            phases=2,
            exec_time=rng.randint(1, 5),
            sleep=rng.choice(SLEEP_KINDS),
            timeout=rng.randint(0, 8),
            # Only used when sleep == "adaptive"; harmless to pass otherwise.
            wma_factor=rng.uniform(1, 40),
            wma_window=rng.randint(1, 6),
            sleep_delay=rng.randint(0, 4),
            wakeup_delay=rng.randint(0, 5),
            exec_power=rng.uniform(0.5, 2.0),
            idle_power=rng.uniform(0.1, 0.5),
            sleep_power=rng.uniform(0.0, 0.05),
        )
    b.sink("snk", exec_time=rng.randint(1, 3))

    def rates(actor_id: str) -> int | list[int]:
        if b.graph.actors[actor_id].kind.value == "phased_rate":
            return [rng.randint(1, 2), rng.randint(0, 2)]
        return rng.randint(1, 2)

    chain = ["src", *stage_ids, "snk"]
    for src, dst in zip(chain, chain[1:]):
        b.connect(
            src,
            dst,
            produce=rates(src),
            consume=rates(dst),
            capacity=rng.choice((None, rng.randint(4, 20))),
        )
    # Occasionally close a feedback loop with initial tokens, which exercises
    # backpressure and bootstrapping together.
    if depth >= 2 and rng.random() < 0.4:
        b.connect(
            stage_ids[-1],
            stage_ids[0],
            capacity=rng.randint(2, 8),
            initial_tokens=rng.randint(1, 2),
        )
    return b.build()


@pytest.mark.parametrize("seed", range(30))
def test_engines_agree_on_random_graphs(seed):
    rng = random.Random(seed)
    graph = random_graph(rng)
    sim = Simulator(graph, seed=seed)
    tick = sim.run(2000, mode="tick")
    event = sim.run(2000, mode="event")

    assert tick.trace["actors"] == event.trace["actors"]
    assert tick.trace["channels"] == event.trace["channels"]
    assert tick.metrics.to_dict() | {"engine": "", "wall_time": 0} == (
        event.metrics.to_dict() | {"engine": "", "wall_time": 0}
    )


@pytest.mark.parametrize("seed", range(6))
def test_engines_agree_over_a_long_run(seed):
    rng = random.Random(1000 + seed)
    graph = random_graph(rng)
    sim = Simulator(graph, seed=seed, trace=TraceConfig(enabled=False))
    tick = sim.run(20_000, mode="tick").metrics
    event = sim.run(20_000, mode="event").metrics
    assert tick.energy == pytest.approx(event.energy)
    assert tick.throughput == event.throughput
    assert [a.firings for a in tick.actors] == [a.firings for a in event.actors]


def test_event_engine_handles_ten_million_cycles():
    b = GraphBuilder("long")
    b.source("src", interval=97, exec_time=3)
    previous = "src"
    for i in range(18):
        aid = f"a{i}"
        b.actor(aid, exec_time=2 + (i % 4), sleep="timeout", timeout=5 + i,
                sleep_delay=3, wakeup_delay=4)
        b.connect(previous, aid, capacity=8)
        previous = aid
    b.sink("snk", exec_time=1)
    b.connect(previous, "snk", capacity=8)
    graph = b.build()

    started = time.perf_counter()
    result = Simulator(graph, trace=TraceConfig(enabled=False)).run(
        10_000_000, mode="event"
    )
    elapsed = time.perf_counter() - started

    assert result.metrics.throughput == pytest.approx(1 / 97, rel=0.01)
    assert result.trace["events"] == 0
    assert elapsed < 120, f"event engine took {elapsed:.1f}s for 10M cycles"
