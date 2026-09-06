"""API contract tests for the browser front end's backend."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from dataflow.model import GraphBuilder, graph_to_dict
from dataflow.web.app import app

client = TestClient(app)


def chain_graph() -> dict:
    b = GraphBuilder("web-test")
    b.source("src", interval=4, exec_time=1)
    b.actor("a", exec_time=2, sleep="immediate")
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=4)
    b.connect("a", "snk", capacity=4)
    return graph_to_dict(b.build())


def test_index_and_static_assets_are_served():
    assert client.get("/").status_code == 200
    for asset in ("main.js", "graph.js", "player.js", "app.css", "theme.css",
                  "vendor/cytoscape.min.js"):
        assert client.get(f"/static/{asset}").status_code == 200, asset


def test_defaults_expose_the_python_model_defaults():
    payload = client.get("/api/defaults").json()
    assert payload["timing"]["exec_time"] == 1
    assert payload["power"]["idle_power"] > payload["power"]["sleep_power"]
    assert set(payload["sleep_kinds"]) == {"never", "immediate", "timeout", "adaptive"}
    assert payload["sleep_policy"]["wma_factor"] > 0
    assert payload["sleep_policy"]["wma_window"] >= 1
    strategies = {s["value"]: s for s in payload["adaptive_strategies"]}
    assert strategies["weighted_moving_average"]["label"] == "Weighted moving average"
    assert strategies["custom"]["label"] == "Custom formula"
    assert payload["sleep_policy"]["custom_expression"] == ""
    kinds = {k["value"]: k for k in payload["actor_kinds"]}
    assert kinds["phased_rate"]["label"] == "Phased rate"
    assert kinds["source"]["group"] == "environment"
    assert {g["id"] for g in payload["actor_groups"]} == {"static", "dynamic", "environment"}
    assert "gaussian" in payload["distributions"]


def test_simulate_with_an_adaptive_actor():
    b = GraphBuilder("adaptive-web-test")
    b.source("src", interval=20, exec_time=1)
    b.actor("a", exec_time=1, sleep="adaptive", wma_factor=60, wma_window=3,
            sleep_delay=1, wakeup_delay=1)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=6)
    b.connect("a", "snk", capacity=6)
    graph = graph_to_dict(b.build())

    response = client.post("/api/simulate", json={"graph": graph, "cycles": 1000})
    assert response.status_code == 200
    body = response.json()
    a = next(m for m in body["metrics"]["actors"] if m["id"] == "a")
    assert a["state_cycles"]["sleeping"] > 0
    assert body["metrics"]["deadlock_cycle"] is None


def test_simulate_with_a_custom_adaptive_formula():
    b = GraphBuilder("custom-web-test")
    b.source("src", interval=20, exec_time=1)
    b.actor("a", exec_time=1, sleep="adaptive", adaptive_strategy="custom",
            custom_expression="sleep_delay + wakeup_delay + gaps[-1] // 4",
            wma_window=3, sleep_delay=1, wakeup_delay=1)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=6)
    b.connect("a", "snk", capacity=6)
    graph = graph_to_dict(b.build())

    response = client.post("/api/simulate", json={"graph": graph, "cycles": 2000})
    assert response.status_code == 200
    body = response.json()
    a = next(m for m in body["metrics"]["actors"] if m["id"] == "a")
    assert a["state_cycles"]["sleeping"] > 0
    assert body["metrics"]["deadlock_cycle"] is None


def test_broken_custom_formula_is_rejected_by_both_validate_and_simulate():
    b = GraphBuilder("broken-custom")
    b.source("src", interval=10, exec_time=1)
    b.actor("a", exec_time=1, sleep="adaptive", adaptive_strategy="custom",
            custom_expression="not_a_real_variable")
    b.sink("snk")
    b.connect("src", "a", capacity=4)
    b.connect("a", "snk", capacity=4)
    graph = graph_to_dict(b.build(validate=False))

    validated = client.post("/api/validate", json={"graph": graph}).json()
    assert validated["ok"] is False
    assert any("custom sleep formula" in p for p in validated["problems"])

    response = client.post("/api/simulate", json={"graph": graph, "cycles": 100})
    assert response.status_code == 422
    assert any("custom sleep formula" in p for p in response.json()["detail"]["problems"])


def test_infinite_sleep_idle_ratio_serialises_as_a_json_safe_string():
    """`immediate` sleep can leave an actor's idle_cycles at 0 (it decides to
    sleep within the very cycle it goes idle), which makes sleep_idle_ratio
    genuinely infinite -- see the model-level test for that. JSON's grammar has
    no literal for infinity, and while Python's own (lenient) `json` module
    would silently parse the bare token back into `float('inf')` and mask a
    bug here, a browser's `JSON.parse` rejects it outright and would fail the
    *entire* response, not just this one field. So the check here is on the
    raw bytes: every appearance of "Infinity" must be quote-delimited (a JSON
    string), never a bare token.
    """
    b = GraphBuilder("infinite-ratio")
    b.source("src", interval=40, exec_time=1)
    b.actor("a", exec_time=2, sleep="immediate", sleep_delay=2, wakeup_delay=3)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=4)
    b.connect("a", "snk", capacity=4)
    graph = graph_to_dict(b.build())

    response = client.post("/api/simulate", json={"graph": graph, "cycles": 2000})
    assert response.status_code == 200

    matches = list(re.finditer("Infinity", response.text))
    assert matches, "expected the infinite ratio to appear in the response at all"
    for match in matches:
        start, end = match.span()
        quoted = response.text[start - 1] == '"' and response.text[end] == '"'
        assert quoted, "found a bare `Infinity` token -- invalid JSON, unparseable by JS"

    a = next(m for m in response.json()["metrics"]["actors"] if m["id"] == "a")
    assert a["state_cycles"]["idle"] == 0
    assert a["sleep_idle_ratio"] == "Infinity"


def test_examples_round_trip():
    examples = client.get("/api/examples").json()
    assert examples, "expected the bundled example networks"
    name = examples[0]["name"]
    graph = client.get(f"/api/examples/{name}").json()
    assert graph["actors"] and graph["channels"]
    assert client.post("/api/validate", json={"graph": graph}).json()["ok"]


def test_unknown_example_is_404():
    assert client.get("/api/examples/nope").status_code == 404


def test_validate_reports_problems():
    graph = chain_graph()
    graph["channels"] = []  # everything is now disconnected
    body = client.post("/api/validate", json={"graph": graph}).json()
    assert body["ok"] is False
    assert any("source" in problem for problem in body["problems"])


def test_simulate_returns_metrics_and_a_replayable_trace():
    response = client.post("/api/simulate", json={
        "graph": chain_graph(), "cycles": 500, "engine": "event",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["throughput"] == pytest.approx(0.25, rel=0.05)
    assert body["metrics"]["cycles"] == 500
    assert body["trace"]["actors"]["a"], "actor state timeline is missing"
    assert body["trace"]["last_cycle"] > 0
    # Every recorded state must be one the UI knows how to colour.
    states = {state for series in body["trace"]["actors"].values() for _, state in series}
    assert states <= {"idle", "executing", "shutdown", "sleeping", "wakeup"}
    # The metrics-panel ratios the front end renders per actor.
    a = next(m for m in body["metrics"]["actors"] if m["id"] == "a")
    assert set(a) >= {"wakeups", "sleep_idle_ratio", "wakeup_firing_ratio"}


def test_simulate_honours_the_trace_window():
    body = client.post("/api/simulate", json={
        "graph": chain_graph(), "cycles": 800,
        "trace": {"enabled": True, "start": 100, "end": 200},
    }).json()
    assert body["metrics"]["cycles"] == 800
    assert body["trace"]["first_cycle"] == 100
    assert body["trace"]["last_cycle"] <= 200


def test_trace_size_is_capped_so_a_long_run_cannot_flood_the_browser():
    """A million cycles of a busy network must still return a bounded trace."""
    b = GraphBuilder("busy")
    b.source("src", interval=1, exec_time=1)
    b.actor("a", exec_time=1)
    b.sink("snk", exec_time=1)
    b.connect("src", "a", capacity=4)
    b.connect("a", "snk", capacity=4)
    graph = graph_to_dict(b.build())

    body = client.post("/api/simulate", json={"graph": graph, "cycles": 1_000_000}).json()
    assert body["metrics"]["cycles"] == 1_000_000  # metrics still cover everything
    assert body["trace"]["truncated"] is True
    assert body["trace"]["events"] <= 150_000
    assert sum(len(series) for series in body["trace"]["actors"].values()) <= 200_000


def test_trace_event_cap_is_clamped():
    response = client.post("/api/simulate", json={
        "graph": chain_graph(), "cycles": 10,
        "trace": {"max_events": 10_000_000},
    })
    assert response.status_code == 422  # above the hard ceiling


def test_simulate_rejects_an_invalid_network():
    graph = chain_graph()
    graph["channels"] = []
    response = client.post("/api/simulate", json={"graph": graph, "cycles": 10})
    assert response.status_code == 422
    assert response.json()["detail"]["problems"]


def test_both_engines_agree_over_the_api():
    graph = chain_graph()
    runs = {
        engine: client.post("/api/simulate", json={
            "graph": graph, "cycles": 1000, "engine": engine,
        }).json()
        for engine in ("tick", "event")
    }
    assert runs["tick"]["trace"] == runs["event"]["trace"]
    assert runs["tick"]["metrics"]["energy"] == runs["event"]["metrics"]["energy"]
