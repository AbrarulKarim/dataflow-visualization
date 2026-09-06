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
  flight. It is part of the firing rule: an executing actor is not fireable, and
  becomes fireable again the moment its firing completes and returns the token.
* Input tokens are *checked* at firing start and **consumed at firing end**;
  outputs are produced at firing end. No reservation bookkeeping is needed: a
  channel has exactly one consumer, and that consumer's self-loop stops it from
  starting a second firing, so nothing else can claim those tokens.
* An actor is fireable only when its self-loop token is free **and** every input
  holds enough tokens **and** every output has room for what the firing will
  produce (blocked-on-write). An actor mid-shutdown or mid-wakeup still holds its
  self-loop token, so it counts as fireable: work is available, it simply cannot
  act on it yet.
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
| `adaptive` | shuts down after a threshold computed from the actor's own fireability history; the countdown still resets on becoming fireable, same as `timeout` |

### Adaptive sleep strategies

`adaptive` picks a sub-strategy (`SleepPolicy.adaptive_strategy`); more can be
added later without touching the ones already there. The first one:

**Weighted moving average** — `delay = X × execution time / (average of the
last N fireability gaps)`, with `X` (`wma_factor`) and `N` (`wma_window`) as
per-actor parameters. `N` only bounds how many gaps the average is taken over;
it does not itself appear in the formula.
A *fireability gap* is the number of cycles between two successive moments the
actor became fireable (the trace's fireable-arrow events, [below](#power-profiles))
— the interval work arrives on, independent of whether the actor was awake to
act on it. Frequent arrivals shrink the average gap, which *raises* the
threshold, so the actor waits longer before committing to a sleep/wake round
trip it would likely have to reverse almost immediately; rare arrivals raise
the average gap, lower the threshold, and send it to sleep sooner. Before `N`
gaps have been observed — at the very start of a run — it falls back to the
plain `timeout` field as a bootstrap value.

Because this threshold can be fractional, the event engine cannot skip straight
to `idle_since + timeout`: it ceils to the first integer cycle at which
`idle_cycles >= timeout` actually holds, which is exactly what the tick engine
would find by checking every cycle. Both engines are checked for exact
agreement on graphs using this policy, same as everywhere else.

### Energy

An actor occupies exactly one state per cycle, so
`energy = Σ power(state) × cycles_in_state`. Sources and sinks are test-bench
infrastructure: they never sleep and their energy is reported separately from
the global compute total.

### Per-actor metrics

Alongside firings, utilization and energy, the Metrics panel (and the JSON/CSV
export) reports two ratios aimed at judging a sleep policy rather than raw
activity:

* **sleep/idle** — sleeping cycles divided by idle cycles. High means the actor
  spends its non-executing time asleep rather than sitting idle. An `immediate`
  policy decides to sleep within the very cycle it goes idle, so it is never
  charged any idle time at all — `idle` is 0, and the ratio is a genuine `∞`
  rather than an undefined fallback: sleeping isn't merely large relative to
  idling here, there is no idling to compare it to. The 0/0 case (never idle
  *and* never asleep, e.g. `never` or a source/sink) does fall back to 0.
* **wakeups/firings** — wakeup transitions divided by firings. Close to 1 means
  almost every firing was preceded by waking up from sleep, i.e. the actor
  rarely catches two arrivals back to back without sleeping in between; well
  below 1 means it's mostly firing back-to-back while awake. A wakeup is
  counted the instant sleep ends, whether or not the actor visibly passes
  through the WAKEUP state on the way (a zero-length wakeup delay skips it),
  so it always pairs with the firing it enabled.

JSON has no literal for infinity — a bare `Infinity` token is invalid syntax
that a browser's `JSON.parse` refuses outright, unlike Python's own (lenient)
`json` module — so an infinite `sleep/idle` is sent as the string `"Infinity"`
over the API and in `--json` output, and rendered as `∞` rather than run
through ordinary number formatting. The CSV export writes the same text.

## Power profiles

The **Power** tab draws one chart per actor: cycles across, power up, and the
area under the step function shaded with the colour of the state the actor was
in. A dashed line marks the actor's mean power over the window, the header gives
peak / mean / energy, and a playhead tracks the transport so the chart and the
canvas stay in step. A ribbon under the axis repeats the state timeline at
constant height — sleep power is often a fiftieth of execution power, so those
stretches would otherwise be a hairline in a true-area plot.

**Arrows above each plot mark the cycles the actor became fireable** — the moment
work became available, which is not the moment it starts running. The gap between
an arrow and the execution block after it is what waking up cost: a wakeup ramp,
or a whole sleep-and-wake round trip if the work arrived mid-shutdown.

Fireability is computed by the simulator, not re-derived in the browser, and only
rising edges are marked. Because the self-loop token is part of the firing rule, an
actor running back-to-back becomes fireable again at each completion, so it gets
**one arrow per firing** — the boundary where the self-loop token comes back —
rather than one for the whole run of firings. **Hide fireable marks** turns them
off; when a view packs them closer than a few pixels the chart draws what it can
and says how many it left out.

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
with **Collapse all** / **Expand all** for the whole list. Profiles are reordered
by dragging the ⠿ handle (or focusing it and pressing ↑ / ↓), which is easiest
with everything collapsed. Order, heights and collapsed rows all survive a
re-run, so the layout you set up stays put while you sweep parameters, and the
SVG export follows the order on screen.

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

## Example networks

`examples/` holds three small networks that isolate one mechanism each — `chain`
(one stage per sleep policy), `feedback` (a credit loop that sets the throughput)
and `phased` (a cyclo-static resampler) — plus eight drawn from real applications.
Rates are the genuine token counts of each application; execution times and powers
are in the framework's relative units, chosen so the balance between stages
resembles a real implementation rather than claiming to measure one.

### Pipelines

| Network | What it exercises |
|---|---|
| `h264-decoder` | H.264 baseline decode at macroblock-row granularity: 9 rows per frame, a reference-frame loop from the deblocking filter back into motion compensation, and a variable-bitrate (gaussian) source. The loop is what stops the pipeline running ahead of itself. |
| `ofdm-receiver` | 802.11a/g baseband receive: 80 samples per symbol (64 + cyclic prefix) down to 48 subcarriers, 4 bits per 16-QAM symbol, rate-1/2 coding, with pilot-driven channel estimation feeding back into the equaliser. Viterbi sits at ~87% occupancy — push its execution time past the 80-cycle symbol period and backpressure throttles everything behind it. |
| `sensor-node` | A duty-cycled IoT node with exponential event arrivals: the case sleep policies exist for. The radio dominates the energy and wakes slowly (400 cycles), so whether it should sleep between events is a real trade-off. Set its policy to `never` and compare. |
| `radar-doppler` | Pulse-Doppler front end built around a phased-rate corner turn — the textbook cyclo-static actor: one pulse of 64 range gates on each of four phases, then the assembled 4×64 block on the last, so the Doppler FFT sees range-Doppler matrices. |

### Networks whose topology is the point

These are less a line and more a graph: branches that rejoin, loops nested inside
loops, and more than one source.

| Network | Topology |
|---|---|
| `hevc-encoder` | A **fork-join inside two nested loops**. Each of 16 CTUs per frame is tried both ways at once — motion estimation against the reference frame and intra prediction from its neighbours — and the mode decision joins the branches. The **reconstruction loop** quantises, inverts everything again, filters, and parks the result in the decoded picture buffer that motion estimation searches next frame; the **rate-control loop** feeds the frame's actual bit cost back into the quantiser. Motion estimation sits at ~48% occupancy, as it does in a real encoder. |
| `turbo-decoder` | An **iteration loop** where the loop *is* the algorithm: two SISO decoders trade extrinsic information through an interleaver, eight times per code block, before a hard decision comes out. The iteration count lives in the rates rather than in control flow — both decoders are phased-rate actors with eight phases, `siso_a` taking the block in on its first and `siso_b` emitting a decision on its last, with one token in the loop at reset. (A CRC-driven stopping rule would need the dynamic actors.) |
| `echo-canceller` | **Two live sources** — the far-end signal and the microphone hearing it back — joining at the subtractor, closed by the **LMS adaptation loop**: the residual error and the far-end reference update the filter taps, so what the filter does next depends on how wrong it was last time. |
| `slam-frontend` | **Two loops running eight apart**, plus two sensors at different rates. Feature matching carries one frame of delay against the previous frame's descriptors; loop-closure detection batches eight keyframes before correcting the map. The map has to *take* its correction on the same eighth-keyframe cadence the outer loop produces it on — consuming one every frame is rate-inconsistent and deadlocks on the second keyframe. |

`tests/test_examples.py` runs every bundled network for 200k cycles and checks it
validates, never deadlocks, keeps every bounded FIFO inside its capacity, agrees
across both engines, and settles into a bounded steady state — which is what
catches a rate typo, since an inconsistent graph either stalls or grows a buffer
without limit.

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
data-dependent firing rules). The firing-rule interface in
`dataflow/sim/core.py` (`fireable` plus the consume/produce snapshot taken at
firing start) is the seam they plug into.

The adaptive sleep policy currently has one strategy (weighted moving
average); further history-based strategies are a matter of adding a branch to
`SleepPolicy.effective_timeout` and an entry to `ADAPTIVE_STRATEGY_INFO` in
`dataflow/model/actor.py` — the UI's strategy picker and the event engine's
deadline prediction both read from that registry rather than hard-coding the
one strategy.
