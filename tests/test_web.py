"""API contract tests for the browser front end's backend."""

from __future__ import annotations

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
    kinds = {k["value"]: k for k in payload["actor_kinds"]}
    assert kinds["phased_rate"]["label"] == "Phased rate"
    assert kinds["source"]["group"] == "environment"
    assert {g["id"] for g in payload["actor_groups"]} == {"static", "dynamic", "environment"}
    assert "gaussian" in payload["distributions"]


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
