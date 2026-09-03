# Dataflow Visualizer

A dataflow modelling, simulation and visualisation framework for exploring
throughput / power / energy trade-offs.

Networks are built in the browser (or loaded from JSON), simulated
cycle-accurately in Python, and replayed cycle-by-cycle with actors coloured by
their power state and live token counts on every channel.

States are colour-coded on the canvas: pastel yellow = idle, blue = executing,
lavender = shutting down, mint = sleeping, salmon = waking up.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Run

Start the web interface:

```bash
.venv/bin/python -m uvicorn dataflow.web.app:app --port 8000
```

Then open <http://localhost:8000>. Or run headless, which is what you want for
parameter sweeps:

```bash
.venv/bin/python -m dataflow.cli examples/chain.dfg.json --cycles 1e6 --csv out.csv
```

## The model

### Actors

Actor kinds are named for their **rate behaviour**, not for a model of
computation. SDF, HSDF and CSDF describe whole graphs, and an SDF graph may
perfectly well contain an actor whose rates happen to be unit — calling that
actor "HSDF" is the confusion this naming avoids.

| Kind | Group | Firing rule |
|---|---|---|
| `static_rate` | static | fixed token counts per firing |
| `unit_rate` | static | exactly one token per port |
| `phased_rate` | static | a rate pattern cycling through `phases` (cyclo-static) |
| `source` | environment | generates tokens on an interval drawn from a distribution |
| `sink` | environment | consumes tokens and measures throughput |

A third group, **dynamic**, is reserved for data-dependent firing rules and is
empty for now. Files written before this rename still load: `sdf`, `hsdf` and
`csdf` are accepted as aliases and rewritten under the current names on save.

Every actor carries a power model (`exec`, `idle`, `sleep`, `shutdown`,
`wakeup`), a timing model (`exec_time`, `sleep_delay`, `wakeup_delay`) and a
sleep policy. All of them have defaults and are editable per actor.

### Firing semantics

* Each actor has an implicit **self-loop holding one token**. It is taken when a
  firing starts and returned when it ends, so an actor never has two firings in
  flight.
* Input tokens are *checked* at firing start and **consumed at firing end**;
  outputs are produced at firing end. No reservation bookkeeping is needed: a
  channel has exactly one consumer, and that consumer's self-loop stops it from
  starting a second firing, so nothing else can claim those tokens.
* An actor is fireable only when every input holds enough tokens **and** every
  output has room for what the firing will produce (blocked-on-write).
* Every actor that is fireable at a cycle boundary starts in that cycle; ties
  are broken by a stable actor ordering, so runs are reproducible.

### Sleep state machine

```
IDLE --(policy fires)--> SHUTDOWN --> SLEEPING --(fireable)--> WAKEUP --> IDLE
IDLE --(fireable)------> EXECUTING -----------------------------------> IDLE
```

Transitions are **atomic**. Once shutdown has begun, work arriving mid-transition
does not abort it: the actor still reaches SLEEPING and then pays the full
wakeup delay. That penalty is the whole point of the sleep policies:

| Policy | Behaviour |
|---|---|
| `never` | stays idle |
| `immediate` | shuts down the cycle it becomes non-fireable |
| `timeout(t)` | shuts down after `t` idle cycles; becoming fireable **resets** the countdown |
| `adaptive` | reserved for history-based strategies; currently behaves as a timeout |

### Energy

An actor occupies exactly one state per cycle, so
`energy = Σ power(state) × cycles_in_state`. Sources and sinks are test-bench
infrastructure: they never sleep and their energy is reported separately from
the global compute total.

## Power profiles

The **Power** tab draws one chart per actor: cycles across, power up, and the
area under the step function shaded with the colour of the state the actor was
in. A dashed line marks the actor's mean power over the window, the header gives
peak / mean / energy, and a playhead tracks the transport so the chart and the
canvas stay in step. A ribbon under the axis repeats the state timeline at
constant height — sleep power is often a fiftieth of execution power, so those
stretches would otherwise be a hairline in a true-area plot.

Each chart zooms and pans on its own, like a waveform viewer: **scroll** to zoom
around the cursor, **drag** to pan, **double-click** (or ⤢) to fit the whole
window, and ⇉ to copy one chart's range onto every other actor so they can be
compared over the same slice. Cycle stamps along the axis re-scale with the view
— 250-cycle steps at full extent, single cycles when zoomed right in — and the
peak / mean / energy figures follow the visible range, so selecting a burst tells
you what that burst cost. The playhead sits on the cycle boundary, lined up with
that cycle's stamp. Scrolling out at full extent hands the wheel back to the
panel, so the list still scrolls.

For a closer look, **⛶** in the panel tab bar expands the panel to the full page
(Esc, or switching to Build, returns); each chart's **S / M / L / XL** button
sets its own height, so one actor can be given room without shrinking the rest;
and the chevron (or the actor's name) collapses a profile to a one-line summary,
with **Collapse all** / **Expand all** for the whole list. Heights and collapsed
rows survive a re-run, so the layout you set up stays put while you sweep
parameters.

Charts are drawn at the pixel size they actually occupy — widening the panel adds
resolution rather than magnifying everything — so a chart goes from about 490px
docked to 1300px across on a full-page 1400px window.

Charts are computed in the browser from the trace and the actor's power model,
so they cost no extra simulation. **Export SVG** writes all the charts to one
standalone file at their current sizes.

### Staying within memory

A trace can hold hundreds of thousands of state changes, so nothing scales a
count of DOM nodes with a count of events:

* the server caps a response at 150k events (hard ceiling 250k) and flags the
  trace as truncated — metrics still cover every cycle;
* each actor's timeline lives in two typed arrays, about 5 bytes per segment
  instead of ~100 for an object per segment;
* the shaded area is drawn as one `<path>` per state, so a chart is ~30 SVG
  nodes whatever it shows;
* a view denser than the chart is wide is bucketed to one bar per column, each
  showing its column's mean power coloured by the state that dominated it (the
  chart says when this is happening — zoom in for exact steps);
* per-render scratch space is reused, grown only when a chart widens, and
  redraws are coalesced into animation frames, so a fast wheel or a window
  resize cannot queue up work.

A 1M-cycle run of a busy network renders all five charts in about 19 MB of JS
heap, with 290 DOM nodes in the panel.

## Engines

Two engines share one semantic core (`dataflow/sim/core.py`) and differ only in
how they pick the next timestamp:

* **tick** — walks every cycle. The semantic reference.
* **event** — jumps straight to the next cycle at which anything can change.
  Handles 10M cycles in seconds.

They are required to produce byte-identical traces and metrics; `tests/test_engines.py`
checks that on 30 randomly generated networks.

## Trace window

Metrics always cover the whole run. The *visual* trace is what gets replayed in
the browser and drives the power charts, and a 10M-cycle run cannot be replayed,
so the trace can be limited to a window (`--trace-start` / `--trace-end`, or the
Run panel fields). Beyond `max_events` the recorder stops and flags the trace as
truncated.

A windowed trace opens with the state everything actually held at that cycle —
the recorder is primed when the run *reaches* the window, not before it starts —
and runs to the end of the window even if nothing changes in the tail.

## Scripting

```python
from dataflow import GraphBuilder, simulate

b = GraphBuilder("sweep")
b.source("src", interval=12, exec_time=1)
b.actor("work", kind="static_rate", exec_time=4, sleep="timeout", timeout=6,
        sleep_delay=3, wakeup_delay=5, exec_power=1.4, idle_power=0.4)
b.sink("snk", exec_time=1)
b.connect("src", "work", capacity=8)
b.connect("work", "snk", capacity=8)
graph = b.build()

for timeout in range(0, 20, 2):
    graph.actors["work"].sleep_policy.timeout = timeout
    result = simulate(graph, 100_000)
    print(timeout, result.metrics.energy, result.metrics.throughput)
```

`examples/make_examples.py` regenerates the bundled networks and doubles as a
tour of the builder API.

## File format

`.dfg.json`, schema version 1: a `name`, a list of `actors` (id, kind, phases,
position, power, timing, sleep_policy, optional distribution) and a list of
`channels` (id, src, dst, capacity, initial_tokens, production_rate,
consumption_rate). Rates are integers, or lists for cyclo-static patterns.
Round-trips exactly through the browser editor.

## Layout

```
dataflow/
  model/   actors, channels, graph validation, JSON io + builder API
  sim/     shared core, tick and event engines, trace recorder, metrics
  web/     FastAPI backend and the static editor/player/charts
  cli.py   headless runner
examples/  bundled networks and their generator
tests/     model, simulation, cross-engine and API tests
```

## Tests

```bash
.venv/bin/python -m pytest
```

## Not yet implemented

Dynamic models of computation (KPN, boolean dataflow, Dennis dataflow,
data-dependent firing rules) and history-based adaptive sleep policies. The
firing-rule interface in `dataflow/sim/core.py` (`fireable` plus the
consume/produce snapshot taken at firing start) is the seam they plug into.
