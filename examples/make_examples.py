"""Regenerate the example networks. Doubles as a tour of the builder API.

    python examples/make_examples.py
"""

from __future__ import annotations

from pathlib import Path

from dataflow.model import GraphBuilder, save_graph

HERE = Path(__file__).parent


def chain() -> GraphBuilder:
    """A slow source feeding a three-stage pipeline, one stage per sleep policy."""
    b = GraphBuilder("pipeline")
    b.source("src", interval=12, exec_time=1, position=(80, 200))
    b.actor("decode", exec_time=3, sleep="immediate", sleep_delay=2, wakeup_delay=3,
            exec_power=1.4, idle_power=0.4, sleep_power=0.02, position=(300, 200))
    b.actor("filter", exec_time=4, sleep="timeout", timeout=6, sleep_delay=3,
            wakeup_delay=5, exec_power=1.1, idle_power=0.35, position=(520, 200))
    b.actor("encode", exec_time=2, sleep="never",
            exec_power=0.9, idle_power=0.3, position=(740, 200))
    b.sink("snk", exec_time=1, position=(960, 200))
    b.connect("src", "decode", capacity=8)
    b.connect("decode", "filter", capacity=8)
    b.connect("filter", "encode", capacity=8)
    b.connect("encode", "snk", capacity=8)
    return b


def feedback() -> GraphBuilder:
    """A self-timed cycle: throughput is set by the loop, not by the source."""
    b = GraphBuilder("feedback-loop")
    b.source("src", interval=5, exec_time=1, position=(80, 240))
    b.actor("split", exec_time=2, sleep="timeout", timeout=4, position=(300, 240))
    b.actor("work", exec_time=3, sleep="immediate", position=(540, 160))
    b.actor("merge", exec_time=2, sleep="timeout", timeout=8, position=(780, 240))
    b.sink("snk", exec_time=1, position=(1000, 240))
    b.connect("src", "split", capacity=6)
    b.connect("split", "work", capacity=4)
    b.connect("work", "merge", capacity=4)
    b.connect("merge", "snk", capacity=6)
    # Credit loop: `split` may only run while `merge` has returned a token.
    b.connect("merge", "split", capacity=3, initial_tokens=2, channel_id="credit")
    return b


def csdf() -> GraphBuilder:
    """A phased-rate stage: consumes 1,3 and produces 2,0 on alternating phases."""
    b = GraphBuilder("cyclo-static")
    b.source("src", interval=3, distribution="gaussian", stddev=1.0, exec_time=1,
             position=(80, 200))
    b.actor("resample", kind="phased_rate", phases=2, exec_time=2, sleep="timeout",
            timeout=5, position=(340, 200))
    b.actor("post", exec_time=1, sleep="immediate", position=(600, 200))
    b.sink("snk", exec_time=1, position=(840, 200))
    b.connect("src", "resample", consume=[1, 3], capacity=12)
    b.connect("resample", "post", produce=[2, 0], capacity=12)
    b.connect("post", "snk", capacity=12)
    return b


EXAMPLES = {"chain": chain, "feedback": feedback, "phased": csdf}


def main() -> None:
    for name, factory in EXAMPLES.items():
        graph = factory().build()
        path = HERE / f"{name}.dfg.json"
        save_graph(graph, path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
