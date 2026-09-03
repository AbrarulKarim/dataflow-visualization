"""Model-level tests: FIFO behaviour, validation and persistence."""

from __future__ import annotations

import json

import pytest

from dataflow.model import (
    Actor,
    ActorKind,
    Channel,
    DataflowGraph,
    Distribution,
    GraphBuilder,
    ValidationError,
    graph_from_dict,
    graph_to_dict,
    load_graph,
    save_graph,
)
import random


def test_channel_capacity_and_underflow():
    chan = Channel(id="c", src="a", dst="b", capacity=3, initial_tokens=1)
    assert chan.tokens == 1
    assert chan.has_space(2) and not chan.has_space(3)
    chan.push(2)
    assert chan.tokens == 3
    with pytest.raises(RuntimeError):
        chan.push(1)
    chan.pop(3)
    with pytest.raises(RuntimeError):
        chan.pop(1)


def test_unbounded_channel_has_infinite_space():
    chan = Channel(id="c", src="a", dst="b", capacity=None)
    assert chan.has_space(10**9)
    assert chan.space == float("inf")


def test_csdf_rates_cycle_through_phases():
    chan = Channel(id="c", src="a", dst="b",
                   production_rate=[1, 2, 0], consumption_rate=3)
    assert [chan.prod_rate(p) for p in range(6)] == [1, 2, 0, 1, 2, 0]
    assert chan.cons_rate(5) == 3
    assert chan.prod_phases() == 3 and chan.cons_phases() == 1


def test_validation_flags_bad_topology():
    graph = DataflowGraph()
    graph.add_actor(Actor(id="src", kind=ActorKind.SOURCE))
    graph.add_actor(Actor(id="snk", kind=ActorKind.SINK))
    graph.add_channel(Channel(id="c1", src="snk", dst="src"))  # backwards
    problems = graph.validate()
    assert any("source" in p for p in problems)
    assert any("sink" in p for p in problems)


def test_validation_rejects_capacity_smaller_than_one_production():
    b = GraphBuilder()
    b.source("src")
    b.sink("snk")
    b.connect("src", "snk", produce=4, capacity=2)
    with pytest.raises(ValidationError, match="capacity"):
        b.build()


def test_hsdf_requires_unit_rates():
    b = GraphBuilder()
    b.source("src")
    b.actor("a", kind="unit_rate")
    b.sink("snk")
    b.connect("src", "a", consume=2)
    b.connect("a", "snk")
    with pytest.raises(ValidationError, match="must be 1"):
        b.build()


def test_sources_and_sinks_never_sleep():
    b = GraphBuilder()
    src = b.source("src", sleep="immediate")
    snk = b.sink("snk", sleep="immediate")
    assert not src.can_sleep and not snk.can_sleep


def test_unknown_actor_parameter_is_rejected():
    b = GraphBuilder()
    with pytest.raises(TypeError, match="unknown actor parameters"):
        b.actor("a", nonsense_power=3)


def test_json_round_trip(tmp_path):
    b = GraphBuilder("demo")
    b.source("src", interval=7, distribution="gaussian", stddev=1.5,
             exec_time=2, idle_power=0.11)
    b.actor("a", kind="phased_rate", phases=2, exec_time=3,
            sleep="timeout", timeout=9, sleep_power=0.05, wakeup_delay=4)
    b.sink("snk")
    b.connect("src", "a", capacity=8, initial_tokens=2)
    b.connect("a", "snk", produce=[1, 2], consume=1)
    graph = b.build()

    path = tmp_path / "demo.dfg.json"
    save_graph(graph, path)
    reloaded = load_graph(path)

    assert graph_to_dict(graph) == graph_to_dict(reloaded)
    assert reloaded.actors["a"].sleep_policy.timeout == 9
    assert reloaded.actors["a"].phases == 2
    assert reloaded.actors["src"].distribution.kind == "gaussian"
    assert reloaded.channels["ch2"].production_rate == [1, 2]
    assert json.loads(path.read_text())["version"] == 1


def test_pre_rename_actor_kinds_still_load():
    """Files written before the rate-based rename must keep working."""
    graph = graph_from_dict({
        "version": 1,
        "name": "legacy",
        "actors": [
            {"id": "src", "kind": "source"},
            {"id": "a", "kind": "sdf"},
            {"id": "b", "kind": "hsdf"},
            {"id": "c", "kind": "csdf", "phases": 2},
            {"id": "snk", "kind": "sink"},
        ],
        "channels": [],
    })
    assert graph.actors["a"].kind is ActorKind.STATIC_RATE
    assert graph.actors["b"].kind is ActorKind.UNIT_RATE
    assert graph.actors["c"].kind is ActorKind.PHASED_RATE
    # ... and are written back out under the current names.
    assert graph_to_dict(graph)["actors"][1]["kind"] == "static_rate"


def test_kind_labels_are_rate_based_not_moc_names():
    assert ActorKind.STATIC_RATE.label == "Static rate"
    assert ActorKind.UNIT_RATE.label == "Unit rate"
    assert ActorKind.PHASED_RATE.label == "Phased rate"
    assert ActorKind.SOURCE.group == "environment"
    assert ActorKind.STATIC_RATE.group == "static"


def test_builder_accepts_both_old_and_new_kind_names():
    b = GraphBuilder()
    assert b.actor("old", kind="csdf", phases=2).kind is ActorKind.PHASED_RATE
    assert b.actor("new", kind="phased_rate", phases=2).kind is ActorKind.PHASED_RATE
    with pytest.raises(ValueError):
        b.actor("bogus", kind="not_a_kind")


def test_future_schema_version_is_refused():
    with pytest.raises(ValueError, match="schema version"):
        graph_from_dict({"version": 99, "actors": [], "channels": []})


def test_distributions_are_seeded_and_reproducible():
    dist = Distribution(kind="gaussian", mean=10, stddev=3)
    first = [dist.sample(random.Random(42)) for _ in range(5)]
    second = [dist.sample(random.Random(42)) for _ in range(5)]
    assert first == second
    assert all(v >= 1 for v in first)

    uniform = Distribution(kind="uniform", low=2, high=6)
    rng = random.Random(0)
    assert all(2 <= uniform.sample(rng) <= 6 for _ in range(50))

    custom = Distribution(kind="custom", expression="3 + rng.random()")
    assert custom.sample(random.Random(1)) in (3, 4)


def test_removing_an_actor_removes_its_channels():
    b = GraphBuilder()
    b.source("src")
    b.actor("a")
    b.sink("snk")
    b.connect("src", "a")
    b.connect("a", "snk")
    graph = b.build()
    graph.remove_actor("a")
    assert graph.channels == {}
