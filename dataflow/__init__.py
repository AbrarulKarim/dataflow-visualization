"""A dataflow modelling, simulation and visualisation framework.

Static dataflow (SDF / HSDF / CSDF) with per-actor power, sleep policies and a
cycle-accurate simulator, plus a browser front end for building and replaying
networks.
"""

from .model import (
    Actor,
    ActorKind,
    ActorState,
    Channel,
    DataflowGraph,
    GraphBuilder,
    PowerModel,
    SleepPolicy,
    TimingModel,
    load_graph,
    save_graph,
)
from .sim import RunResult, Simulator, TraceConfig, simulate

__version__ = "0.1.0"

__all__ = [
    "Actor",
    "ActorKind",
    "ActorState",
    "Channel",
    "DataflowGraph",
    "GraphBuilder",
    "PowerModel",
    "RunResult",
    "SleepPolicy",
    "Simulator",
    "TimingModel",
    "TraceConfig",
    "load_graph",
    "save_graph",
    "simulate",
]
