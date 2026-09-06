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
    SleepPolicy,
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


# --------------------------------------------------------------------------- #
# Adaptive sleep policy: weighted moving average
# --------------------------------------------------------------------------- #


def test_adaptive_policy_rejects_bad_parameters():
    with pytest.raises(ValueError, match="window"):
        SleepPolicy(kind="adaptive", wma_window=0)
    with pytest.raises(ValueError, match="factor"):
        SleepPolicy(kind="adaptive", wma_factor=-1)


def test_adaptive_timeout_is_x_times_exec_time_over_the_average_gap():
    policy = SleepPolicy(kind="adaptive", wma_factor=100, wma_window=5)
    assert policy.effective_timeout([10, 20, 30], exec_time=4) == pytest.approx(100 * 4 / 20)
    assert policy.effective_timeout([5], exec_time=1) == pytest.approx(100 * 1 / 5)
    # wma_window bounds how many gaps are averaged (enforced by the bounded
    # history deque in the simulation core); it does not appear in the formula
    # itself, so changing it alone must not change the result for a fixed set
    # of gaps.
    same_gaps = SleepPolicy(kind="adaptive", wma_factor=100, wma_window=2)
    assert same_gaps.effective_timeout([10, 20, 30], exec_time=4) \
        == policy.effective_timeout([10, 20, 30], exec_time=4)


def test_adaptive_timeout_bootstraps_from_the_configured_timeout():
    """No gap history yet -- as at the very start of a run -- falls back."""
    policy = SleepPolicy(kind="adaptive", wma_factor=100, timeout=7)
    assert policy.effective_timeout([]) == 7
    # A degenerate (non-positive) average is equally "no usable signal".
    assert policy.effective_timeout([0, 0]) == 7


def test_should_sleep_adaptive_matches_the_computed_threshold():
    policy = SleepPolicy(kind="adaptive", wma_factor=30)
    gaps = [10, 10, 10]  # average 10, exec_time 5 -> threshold 30*5/10 = 15
    assert not policy.should_sleep(14, gaps, exec_time=5)
    assert policy.should_sleep(15, gaps, exec_time=5)


def test_timeout_and_never_kinds_ignore_adaptive_fields():
    policy = SleepPolicy(kind="timeout", timeout=12, wma_factor=999, wma_window=1)
    assert policy.effective_timeout([1, 2, 3], exec_time=99) == 12
    assert not SleepPolicy(kind="never", wma_factor=0).should_sleep(10**6, [1], exec_time=99)


# --------------------------------------------------------------------------- #
# Adaptive sleep policy: custom formula
# --------------------------------------------------------------------------- #


def test_custom_formula_sees_the_documented_variables():
    """Every variable promised in the UI hint text must actually be in scope."""
    policy = SleepPolicy(kind="adaptive", adaptive_strategy="custom", timeout=7, wma_window=5)
    ns = policy._custom_namespace([10, 20, 30], exec_time=2, sleep_delay=3, wakeup_delay=4)  # noqa: SLF001
    assert ns["exec_time"] == 2
    assert ns["sleep_delay"] == 3
    assert ns["wakeup_delay"] == 4
    assert ns["timeout"] == 7
    assert ns["window"] == 5
    assert ns["gaps"] == (10, 20, 30)
    assert ns["mean_gap"] == pytest.approx(20)
    # Also confirm each name really is usable inside an expression, not just
    # present in the namespace dict.
    combined = SleepPolicy(
        kind="adaptive", adaptive_strategy="custom", timeout=7, wma_window=5,
        custom_expression="exec_time + sleep_delay + wakeup_delay + timeout + "
        "window + gaps[-1] + mean_gap",
    )
    assert combined.effective_timeout(
        [10, 20, 30], exec_time=2, sleep_delay=3, wakeup_delay=4,
    ) == pytest.approx(2 + 3 + 4 + 7 + 5 + 30 + 20)


def test_custom_formula_computes_a_literal_expression():
    policy = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                          custom_expression="exec_time * 2 + mean_gap")
    assert policy.effective_timeout([4, 6], exec_time=3) == pytest.approx(3 * 2 + 5)


def test_custom_formula_can_index_the_gap_history():
    """"Last Nth fireability gap" -- gaps is a plain indexable sequence."""
    policy = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                          custom_expression="gaps[-1] + gaps[0]")
    assert policy.effective_timeout([5, 8, 13], exec_time=1) == pytest.approx(13 + 5)


def test_custom_formula_has_safe_functions_but_not_real_builtins():
    safe = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                        custom_expression="max(gaps) - min(gaps) + len(gaps) + "
                        "round(math.sqrt(sum(gaps)))")
    assert safe.effective_timeout([2, 4, 10], exec_time=1) == pytest.approx(
        10 - 2 + 3 + round(16 ** 0.5)
    )
    unsafe = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                          custom_expression="__import__('os').system('echo pwned')")
    problem = unsafe.validate_custom_expression()
    assert problem is not None
    assert "not defined" in problem


def test_custom_formula_bootstraps_from_the_configured_timeout():
    policy = SleepPolicy(kind="adaptive", adaptive_strategy="custom", timeout=11,
                         custom_expression="1 / 0")  # would explode if it ever ran
    assert policy.effective_timeout([]) == 11


def test_custom_formula_falls_back_to_timeout_on_a_runtime_error():
    """Indexing past what history has *actually* accumulated so far (as opposed
    to what the window will eventually hold) is a transient condition, not a
    permanent bug -- it degrades to the bootstrap timeout rather than raising."""
    policy = SleepPolicy(kind="adaptive", adaptive_strategy="custom", timeout=9,
                         wma_window=5, custom_expression="gaps[-5]")
    assert policy.effective_timeout([1, 2], exec_time=1) == 9  # only 2 of 5 gaps so far


def test_validate_custom_expression_catches_permanent_problems():
    unknown_name = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                               custom_expression="not_a_real_variable")
    assert unknown_name.validate_custom_expression() is not None

    bad_syntax = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                             custom_expression="mean_gap + ")
    assert bad_syntax.validate_custom_expression() is not None

    empty = SleepPolicy(kind="adaptive", adaptive_strategy="custom", custom_expression="  ")
    assert empty.validate_custom_expression() == "custom sleep formula is empty"

    # gaps[-6] can never succeed with a window of 5: even a full window only
    # ever holds 5 elements, so this is a permanent bug, not a transient one --
    # validation (a full-size synthetic sample) must catch it up front, unlike
    # the transient case in test_custom_formula_falls_back_to_timeout_on_a_
    # runtime_error above (which validates fine, since it *can* eventually work).
    always_out_of_range = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                                      wma_window=5, custom_expression="gaps[-6]")
    assert always_out_of_range.validate_custom_expression() is not None

    valid = SleepPolicy(kind="adaptive", adaptive_strategy="custom",
                        wma_window=5, custom_expression="gaps[-5] + mean_gap")
    assert valid.validate_custom_expression() is None


def test_non_custom_policies_have_nothing_to_validate():
    for policy in (
        SleepPolicy(kind="never"),
        SleepPolicy(kind="timeout", timeout=5),
        SleepPolicy(kind="adaptive", adaptive_strategy="weighted_moving_average"),
    ):
        assert policy.validate_custom_expression() is None


def test_graph_validation_surfaces_a_broken_custom_formula():
    b = GraphBuilder()
    b.source("src", interval=10, exec_time=1)
    b.actor("a", exec_time=1, sleep="adaptive", adaptive_strategy="custom",
            custom_expression="this_name_does_not_exist")
    b.sink("snk")
    b.connect("src", "a", capacity=4)
    b.connect("a", "snk", capacity=4)
    with pytest.raises(ValidationError, match="custom sleep formula"):
        b.build()


def test_json_round_trip(tmp_path):
    b = GraphBuilder("demo")
    b.source("src", interval=7, distribution="gaussian", stddev=1.5,
             exec_time=2, idle_power=0.11)
    b.actor("a", kind="phased_rate", phases=2, exec_time=3,
            sleep="timeout", timeout=9, sleep_power=0.05, wakeup_delay=4)
    b.actor("adaptive_actor", sleep="adaptive", wma_factor=42.5, wma_window=6)
    b.actor("custom_actor", sleep="adaptive", adaptive_strategy="custom",
            custom_expression="wakeup_delay + mean_gap / 2", wma_window=3)
    b.sink("snk")
    b.connect("src", "a", capacity=8, initial_tokens=2)
    b.connect("a", "adaptive_actor", produce=[1, 2], consume=1, capacity=8)
    b.connect("adaptive_actor", "custom_actor", capacity=8)
    b.connect("custom_actor", "snk", capacity=8)
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

    adaptive = reloaded.actors["adaptive_actor"].sleep_policy
    assert adaptive.kind == "adaptive"
    assert adaptive.wma_factor == pytest.approx(42.5)
    assert adaptive.wma_window == 6
    assert adaptive.adaptive_strategy == "weighted_moving_average"

    custom = reloaded.actors["custom_actor"].sleep_policy
    assert custom.adaptive_strategy == "custom"
    assert custom.custom_expression == "wakeup_delay + mean_gap / 2"


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
