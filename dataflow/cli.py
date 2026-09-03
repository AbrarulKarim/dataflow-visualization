"""Headless simulation runner -- the entry point for parameter sweeps.

    python -m dataflow.cli examples/chain.dfg.json --cycles 100000
    python -m dataflow.cli examples/chain.dfg.json -c 1e7 --json out.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .model import load_graph
from .sim import Simulator, TraceConfig


def _cycles(text: str) -> int:
    """Accept 100000, 1e7 and 10_000_000 alike."""
    value = float(text.replace("_", ""))
    if value < 0 or value != int(value):
        raise argparse.ArgumentTypeError("cycles must be a non-negative integer")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataflow", description="Simulate a dataflow graph and report metrics."
    )
    parser.add_argument("graph", type=Path, help="a .dfg.json network file")
    parser.add_argument("-c", "--cycles", type=_cycles, default=10_000)
    parser.add_argument(
        "-e", "--engine", choices=("event", "tick"), default="event",
        help="event jumps between state changes; tick is the slow reference",
    )
    parser.add_argument("-s", "--seed", type=int, default=0,
                        help="seed for source inter-arrival distributions")
    parser.add_argument("--trace", action="store_true",
                        help="record the visual trace as well as metrics")
    parser.add_argument("--trace-start", type=_cycles, default=0)
    parser.add_argument("--trace-end", type=_cycles, default=None)
    parser.add_argument("--json", type=Path, metavar="FILE",
                        help="write metrics (and trace, with --trace) as JSON")
    parser.add_argument("--csv", type=Path, metavar="FILE",
                        help="write the per-actor table as CSV")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    graph = load_graph(args.graph)
    problems = graph.validate()
    if problems:
        print("graph is not simulatable:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    trace_config = TraceConfig(
        enabled=args.trace, start=args.trace_start, end=args.trace_end
    )
    result = Simulator(graph, seed=args.seed, trace=trace_config).run(
        args.cycles, mode=args.engine
    )

    if not args.quiet:
        print(result.metrics.summary())
        print(f"\nsimulated in {result.metrics.wall_time:.3f}s")
        if result.metrics.deadlock_cycle is not None:
            print("note: the network deadlocked; figures cover the full window.")

    if args.json:
        payload = result.to_dict() if args.trace else {"metrics": result.metrics.to_dict()}
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        if not args.quiet:
            print(f"wrote {args.json}")

    if args.csv:
        with args.csv.open("w", newline="") as handle:
            fields = ["id", "name", "kind", "firings", "utilization", "energy",
                      "avg_power", "tokens_in", "tokens_out",
                      "idle", "executing", "shutdown", "sleeping", "wakeup"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for actor in result.metrics.actors:
                row = actor.to_dict()
                row.update(row.pop("state_cycles"))
                writer.writerow({k: row[k] for k in fields})
        if not args.quiet:
            print(f"wrote {args.csv}")

    return 1 if result.metrics.deadlock_cycle is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
