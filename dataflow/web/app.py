"""FastAPI backend.

The browser owns no simulation logic: it posts a graph, gets back metrics plus a
change-only trace, and replays that trace locally. Scrubbing and stepping are
therefore instant, and a long run costs exactly one request.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..model import graph_from_dict, graph_to_dict, load_graph
from ..model.actor import KIND_INFO, PowerModel, SleepPolicy, TimingModel
from ..sim import Simulator, TraceConfig

STATIC_DIR = Path(__file__).parent / "static"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"

app = FastAPI(title="Dataflow Visualizer", version="0.1.0")


#: Hard ceiling on the events a single response may carry. Each event costs the
#: browser roughly 100 bytes once parsed into JS objects, so this bounds a trace
#: at a few tens of megabytes however many cycles were asked for.
MAX_TRACE_EVENTS = 250_000


class TraceRequest(BaseModel):
    enabled: bool = True
    start: int = 0
    end: int | None = None
    max_events: int = Field(default=150_000, ge=1, le=MAX_TRACE_EVENTS)


class SimulateRequest(BaseModel):
    graph: dict[str, Any]
    cycles: int = Field(default=1000, ge=0, le=1_000_000_000)
    engine: str = Field(default="event", pattern="^(event|tick)$")
    seed: int = 0
    trace: TraceRequest = Field(default_factory=TraceRequest)


class GraphRequest(BaseModel):
    graph: dict[str, Any]


@app.get("/api/defaults")
def defaults() -> dict[str, Any]:
    """Model defaults, so the UI forms have a single source of truth in Python."""
    return {
        "power": asdict(PowerModel()),
        "timing": asdict(TimingModel()),
        "sleep_policy": asdict(SleepPolicy()),
        "sleep_kinds": ["never", "immediate", "timeout", "adaptive"],
        # The UI builds its actor palette and type menu from this, so the naming
        # and grouping live in one place: the model.
        "actor_kinds": [
            {
                "value": kind.value,
                "label": info.label,
                "group": info.group,
                "summary": info.summary,
            }
            for kind, info in KIND_INFO.items()
        ],
        "actor_groups": [
            {"id": "static", "label": "Static"},
            {"id": "dynamic", "label": "Dynamic"},
            {"id": "environment", "label": "Env"},
        ],
        "distributions": ["constant", "uniform", "gaussian", "exponential", "custom"],
    }


@app.get("/api/examples")
def list_examples() -> list[dict[str, str]]:
    if not EXAMPLES_DIR.is_dir():
        return []
    return [
        {"name": path.name.removesuffix(".dfg.json"), "file": path.name}
        for path in sorted(EXAMPLES_DIR.glob("*.dfg.json"))
    ]


@app.get("/api/examples/{name}")
def get_example(name: str) -> dict[str, Any]:
    path = (EXAMPLES_DIR / f"{name}.dfg.json").resolve()
    if not path.is_file() or EXAMPLES_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail=f"no example named {name!r}")
    return graph_to_dict(load_graph(path))


@app.post("/api/validate")
def validate(request: GraphRequest) -> dict[str, Any]:
    try:
        graph = graph_from_dict(request.graph)
    except Exception as exc:  # malformed payload from the editor
        return {"ok": False, "problems": [str(exc)]}
    problems = graph.validate()
    return {"ok": not problems, "problems": problems}


@app.post("/api/simulate")
def simulate(request: SimulateRequest) -> dict[str, Any]:
    try:
        graph = graph_from_dict(request.graph)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    problems = graph.validate()
    if problems:
        raise HTTPException(status_code=422, detail={"problems": problems})

    trace_config = TraceConfig(**request.trace.model_dump())
    simulator = Simulator(graph, seed=request.seed, trace=trace_config)
    result = simulator.run(request.cycles, mode=request.engine)  # type: ignore[arg-type]
    return result.to_dict()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class RevalidatingStaticFiles(StaticFiles):
    """Serve the editor's assets with ``Cache-Control: no-cache``.

    Without it browsers cache the ES modules heuristically and keep running an
    old build after the files change -- a reload, even a forced one, does not
    reliably rebuild a cached module graph. ``no-cache`` means "revalidate",
    not "do not store": unchanged files still answer 304 from the ETag.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    """Console-script entry point: ``dataflow-server``."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the dataflow visualizer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "dataflow.web.app:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    run()
