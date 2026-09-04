// Per-actor power profiles: cycles across, power up, the area under the step
// function shaded with the colour of the state the actor was in.
//
// Charts are derived in the browser from the trace the backend already sends
// (state changes) plus the actor's power model, so no extra request is needed.
// Each one zooms and pans on its own, waveform-viewer style, and is drawn at the
// pixel size it actually occupies so text stays crisp as the panel grows.
//
// Memory and DOM budget
// ---------------------
// A trace can hold hundreds of thousands of state changes, so nothing here
// scales a *count of DOM nodes* with a count of events:
//
//   * each actor's timeline is kept in two typed arrays (~5 bytes per segment)
//     rather than an array of objects (~100 bytes each);
//   * the shaded area is drawn as one <path> per state -- about 30 nodes per
//     chart regardless of how much detail is on screen;
//   * a view holding more segments than the chart has pixel columns is bucketed
//     into one bar per column, so path data is bounded by the chart's width;
//   * per-render scratch space is reused, and grown only when a chart widens;
//   * redraws are coalesced into animation frames, so a fast scroll wheel or a
//     window resize cannot queue up work.

import { KIND_LABELS, STATES, STATE_COLORS } from './graph.js';

const POWER_KEY = {
  idle: 'idle_power',
  executing: 'exec_power',
  sleeping: 'sleep_power',
  shutdown: 'shutdown_power',
  wakeup: 'wakeup_power',
};

const STATE_INDEX = Object.fromEntries(STATES.map((state, i) => [state, i]));
const SVG_NS = 'http://www.w3.org/2000/svg';

/** Plot margins, in CSS pixels. */
const PAD = { left: 46, right: 14, top: 18, bottom: 42 };
const RIBBON_GAP = 3;
const RIBBON_HEIGHT = 5;

/** Chart heights to cycle through when a profile needs a closer look. */
const HEIGHTS = [
  { name: 'S', px: 100 },
  { name: 'M', px: 150 },
  { name: 'L', px: 240 },
  { name: 'XL', px: 380 },
];
const DEFAULT_HEIGHT = 1;

const FALLBACK_WIDTH = 620;
const MIN_WIDTH = 260;
/** Detail beyond one bar per pixel cannot be seen, so it is never drawn. */
const MAX_COLUMNS = 2400;
/** Never zoom in past this many cycles; below it the step function is moot. */
const MIN_SPAN = 4;
/** Fireable arrows closer together than this (in px) are not worth drawing. */
const MARKER_PITCH = 5;

const css = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
};

const setAttrs = (node, attrs) => {
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
};

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else node.setAttribute(key, String(value));
  });
  children.forEach((child) => node.append(child));
  return node;
};

const grouped = new Intl.NumberFormat();

function tidy(value) {
  if (!Number.isFinite(value)) return '—';
  if (value === 0) return '0';
  const magnitude = Math.abs(value);
  if (magnitude >= 1000) return grouped.format(Math.round(value));
  if (magnitude >= 1) return Number(value.toFixed(3)).toString();
  return Number(value.toPrecision(3)).toString();
}

const cycleLabel = (cycle) => (Math.abs(cycle) >= 10000 ? grouped.format(cycle) : String(cycle));

/**
 * Tick positions for the visible span, chosen from the 1/2/2.5/5 ladder so the
 * stamps stay round as the view is zoomed -- the way a waveform viewer does it.
 */
export function niceTicks(from, to, target = 12) {
  const span = Math.max(1, to - from);
  const rough = span / target;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  // 2.5 only earns a place on the ladder where it lands on a whole cycle.
  const ladder = [1, 2, 2.5, 5, 10]
    .map((rung) => rung * magnitude)
    .filter((candidate) => Number.isInteger(candidate) && candidate >= 1);
  const step = Math.max(1, ladder.find((candidate) => candidate >= rough) ?? 10 * magnitude);

  const ticks = [];
  for (let cycle = Math.ceil(from / step) * step; cycle < to; cycle += step) {
    ticks.push(cycle);
  }
  return ticks;
}

/**
 * Pack a change-only state series into typed arrays: segment `i` runs from
 * `starts[i]` to `starts[i + 1]` (or `end`) in state `states[i]`.
 */
export function buildTimeline(series, from, toExclusive) {
  const starts = [];
  const states = [];
  if (series && series.length && toExclusive > from) {
    let state = series[0][1];
    let start = Math.max(from, series[0][0]);
    for (let i = 1; i < series.length; i += 1) {
      const [cycle, next] = series[i];
      if (cycle >= toExclusive) break;
      if (cycle > start) {
        starts.push(start);
        states.push(STATE_INDEX[state] ?? 0);
        start = cycle;
      }
      state = next;
    }
    if (start < toExclusive) {
      starts.push(start);
      states.push(STATE_INDEX[state] ?? 0);
    }
  }
  return {
    starts: Int32Array.from(starts),
    states: Uint8Array.from(states),
    end: toExclusive,
    count: starts.length,
  };
}

/** Index of the segment covering `cycle` (or the first one after it). */
export function segmentAt(timeline, cycle) {
  const { starts, count } = timeline;
  if (count === 0 || cycle <= starts[0]) return 0;
  let low = 0;
  let high = count - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (starts[mid] <= cycle) low = mid;
    else high = mid - 1;
  }
  return low;
}

const segmentEnd = (timeline, i) =>
  (i + 1 < timeline.count ? timeline.starts[i + 1] : timeline.end);

/** Index of the first entry of a sorted array that is >= `value`. */
export function firstAtLeast(sorted, value) {
  let low = 0;
  let high = sorted.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (sorted[mid] < value) low = mid + 1;
    else high = mid;
  }
  return low;
}

/** Peak, mean and energy over an arbitrary cycle range. */
export function statsOver(timeline, powerAt, from, to) {
  let energy = 0;
  let peak = 0;
  for (let i = segmentAt(timeline, from); i < timeline.count; i += 1) {
    const start = timeline.starts[i];
    if (start >= to) break;
    const overlap = Math.min(segmentEnd(timeline, i), to) - Math.max(start, from);
    if (overlap <= 0) continue;
    const power = powerAt(timeline.states[i]);
    energy += power * overlap;
    peak = Math.max(peak, power);
  }
  const span = Math.max(1, to - from);
  return { peak, mean: energy / span, energy };
}

// --------------------------------------------------------------------------- //
// One chart
// --------------------------------------------------------------------------- //

class PowerChart {
  constructor(actor, series, bounds, colors, viewState, fireable) {
    this.actor = actor;
    this.fireable = fireable || [];
    this.colors = colors;
    this.viewState = viewState;
    this.bounds = bounds; // { from, to } -- the whole trace window
    this.view = { ...bounds };
    this.timeline = buildTimeline(series, bounds.from, bounds.to);
    this.powers = STATES.map((state) => actor.power[POWER_KEY[state]] ?? 0);
    this.powerAt = (index) => this.powers[index];

    this.width = FALLBACK_WIDTH;
    // Height and collapsed-ness survive a re-render (a new run, a theme change).
    this.heightIndex = viewState.heights.get(actor.id) ?? DEFAULT_HEIGHT;
    this.collapsed = viewState.collapsed.has(actor.id);
    this.dirty = false;
    this.frame = null;

    this.buildDom();
    this.render();
  }

  get height() {
    return HEIGHTS[this.heightIndex].px;
  }

  get plot() {
    return {
      x0: PAD.left,
      x1: Math.max(PAD.left + 40, this.width - PAD.right),
      y0: PAD.top,
      y1: this.height - PAD.bottom,
    };
  }

  // -- DOM -----------------------------------------------------------------

  buildDom() {
    const { colors } = this;
    this.root = el('div', { class: 'power-card' });

    const head = el('div', { class: 'power-head' });
    this.grip = el('button', {
      class: 'btn btn-icon chart-grip',
      text: '⠿',
      draggable: 'true',
      title: 'Drag to reorder — or focus this handle and press ↑ / ↓',
      'aria-label': `Reorder ${this.actor.name}`,
    });
    this.chevron = el('button', { class: 'btn btn-icon chart-collapse' });
    this.chevron.addEventListener('click', () => this.setCollapsed(!this.collapsed));
    this.title = el('b', { class: 'power-title', text: this.actor.name });
    this.title.addEventListener('click', () => this.setCollapsed(!this.collapsed));
    const tools = el('div', { class: 'chart-tools' });
    const tool = (label, title, handler) => {
      const button = el('button', { class: 'btn btn-icon', text: label, title });
      button.addEventListener('click', handler);
      return button;
    };
    this.sizeButton = tool(HEIGHTS[this.heightIndex].name, '', () => this.cycleHeight());
    this.sizeButton.classList.add('is-size');
    this.updateSizeButton();
    tools.append(
      this.sizeButton,
      tool('−', 'Zoom out', () => this.zoomBy(1 / 0.6)),
      tool('+', 'Zoom in', () => this.zoomBy(0.6)),
      tool('⤢', 'Fit whole window (double-click the chart)', () => this.reset()),
      tool('⇉', 'Apply this range to every actor', () => this.onBroadcast?.(this.view)),
    );
    head.append(
      this.grip,
      this.chevron,
      this.title,
      el('span', { class: 'pill', text: KIND_LABELS[this.actor.kind] || this.actor.kind }),
      tools,
    );

    this.stats = el('div', { class: 'power-stats' });

    this.svg = svgEl('svg', { class: 'power-svg', 'font-family': colors.font });
    this.gridGroup = svgEl('g', {});
    this.svg.append(this.gridGroup);

    // One path per state for the area, and one per state for the ribbon.
    this.areaPaths = STATES.map((state) => svgEl('path', { fill: STATE_COLORS[state] }));
    this.ribbonPaths = STATES.map((state) => svgEl('path', { fill: STATE_COLORS[state] }));
    this.areaPaths.forEach((path) => this.svg.append(path));

    this.peakLine = svgEl('line', {
      stroke: colors.grid, 'stroke-width': 1, 'stroke-dasharray': '2 3',
    });
    this.meanLine = svgEl('line', {
      stroke: colors.accent, 'stroke-width': 1.2, 'stroke-dasharray': '5 3', opacity: 0.85,
    });
    this.svg.append(this.peakLine, this.meanLine);
    this.ribbonPaths.forEach((path) => this.svg.append(path));

    this.baseAxis = svgEl('line', { stroke: colors.axis, 'stroke-width': 1 });
    this.leftAxis = svgEl('line', { stroke: colors.axis, 'stroke-width': 1 });
    this.svg.append(this.baseAxis, this.leftAxis);

    this.peakLabel = svgEl('text', {
      x: PAD.left - 6, 'text-anchor': 'end', 'font-size': 9, fill: colors.faint,
    });
    this.zeroLabel = svgEl('text', {
      x: PAD.left - 6, 'text-anchor': 'end', 'font-size': 9, fill: colors.faint,
    });
    this.zeroLabel.textContent = '0';
    this.svg.append(this.peakLabel, this.zeroLabel);

    // One path holds every arrow, so the marker layer is a single DOM node.
    this.markers = svgEl('path', { class: 'power-markers' });
    this.svg.append(this.markers);

    this.playhead = svgEl('line', {
      stroke: colors.accent, 'stroke-width': 1.4, class: 'power-playhead', opacity: 0,
    });
    this.svg.append(this.playhead);

    // The SVG is sized in pixels, so it needs a wrapper whose width is decided
    // purely by the panel -- measuring the SVG itself would be circular.
    this.plotBox = el('div', { class: 'power-plot' });
    this.plotBox.append(this.svg);

    this.note = el('p', { class: 'hint', hidden: 'hidden' });
    // Only the plot collapses; the stats line stays as the row's summary.
    this.body = el('div', { class: 'power-body' });
    this.body.append(this.plotBox, this.note);
    this.root.append(head, this.stats, this.body);

    this.attachGestures();
    this.applyCollapsed();
  }

  /** Wheel to zoom around the cursor, drag to pan, double-click to fit. */
  attachGestures() {
    this.onWheel = (event) => {
      const span = this.view.to - this.view.from;
      const zoomingOut = event.deltaY > 0;
      const atFullExtent = span >= this.bounds.to - this.bounds.from;
      // Once the whole window is on screen, hand the wheel back to the panel so
      // the list of charts stays scrollable.
      if (zoomingOut && atFullExtent) return;
      event.preventDefault();
      this.zoomBy(zoomingOut ? 1 / 0.85 : 0.85, this.cycleAt(event));
    };
    this.svg.addEventListener('wheel', this.onWheel, { passive: false });

    this.onPointerDown = (event) => {
      if (event.button !== 0) return;
      this.drag = { x: event.clientX, from: this.view.from, to: this.view.to };
      this.svg.setPointerCapture(event.pointerId);
      this.svg.classList.add('is-panning');
    };
    this.onPointerMove = (event) => {
      if (!this.drag) return;
      const { x0, x1 } = this.plot;
      const perPixel = (this.drag.to - this.drag.from) / Math.max(1, x1 - x0);
      const shift = (this.drag.x - event.clientX) * perPixel;
      this.setView(this.drag.from + shift, this.drag.to + shift);
    };
    this.onPointerUp = (event) => {
      if (!this.drag) return;
      this.drag = null;
      this.svg.releasePointerCapture(event.pointerId);
      this.svg.classList.remove('is-panning');
    };
    this.onDoubleClick = () => this.reset();

    this.svg.addEventListener('pointerdown', this.onPointerDown);
    this.svg.addEventListener('pointermove', this.onPointerMove);
    this.svg.addEventListener('pointerup', this.onPointerUp);
    this.svg.addEventListener('pointercancel', this.onPointerUp);
    this.svg.addEventListener('dblclick', this.onDoubleClick);
  }

  destroy() {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.svg.removeEventListener('wheel', this.onWheel);
    this.svg.removeEventListener('pointerdown', this.onPointerDown);
    this.svg.removeEventListener('pointermove', this.onPointerMove);
    this.svg.removeEventListener('pointerup', this.onPointerUp);
    this.svg.removeEventListener('pointercancel', this.onPointerUp);
    this.svg.removeEventListener('dblclick', this.onDoubleClick);
    this.timeline = null;
    this.colTotal = this.colEnergy = this.colHeld = null;
  }

  setCollapsed(on) {
    this.collapsed = on;
    if (on) this.viewState.collapsed.add(this.actor.id);
    else this.viewState.collapsed.delete(this.actor.id);
    this.applyCollapsed();
    this.onFold?.();
    if (!on && this.dirty) {
      this.dirty = false;
      this.render();
    }
  }

  /**
   * Draw into the (possibly hidden) SVG even while collapsed. Collapsed charts
   * skip rendering to stay cheap, so anything that reads their SVG -- the SVG
   * export -- has to ask for the pixels first.
   */
  ensureDrawn() {
    if (!this.collapsed && !this.dirty) return;
    const collapsed = this.collapsed;
    this.collapsed = false;
    this.render();
    this.collapsed = collapsed;
    this.dirty = false;
  }

  applyCollapsed() {
    this.root.classList.toggle('is-collapsed', this.collapsed);
    this.body.hidden = this.collapsed;
    this.chevron.textContent = this.collapsed ? '▸' : '▾';
    this.chevron.title = this.collapsed ? 'Expand this profile' : 'Collapse this profile';
  }

  // -- size ----------------------------------------------------------------

  setWidth(width) {
    const next = Math.round(width);
    // A hidden panel measures 0; keep the last good width until it is shown.
    if (next < MIN_WIDTH || Math.abs(next - this.width) < 2) return;
    this.width = next;
    this.scheduleRender();
  }

  cycleHeight() {
    this.heightIndex = (this.heightIndex + 1) % HEIGHTS.length;
    this.viewState.heights.set(this.actor.id, this.heightIndex);
    this.updateSizeButton();
    if (this.collapsed) this.setCollapsed(false);
    else this.scheduleRender();
  }

  updateSizeButton() {
    const preset = HEIGHTS[this.heightIndex];
    this.sizeButton.textContent = preset.name;
    this.sizeButton.title = `Chart height: ${preset.name} — click for a taller profile`;
  }

  // -- view ----------------------------------------------------------------

  /** Cycle under a pointer event, in the chart's own coordinates. */
  cycleAt(event) {
    const { x0, x1 } = this.plot;
    const rect = this.svg.getBoundingClientRect();
    const ratio = (event.clientX - rect.left - x0) / Math.max(1, x1 - x0);
    return this.view.from + Math.min(1, Math.max(0, ratio)) * (this.view.to - this.view.from);
  }

  setView(from, to) {
    const limit = this.bounds;
    const whole = limit.to - limit.from;
    const span = Math.min(whole, Math.max(MIN_SPAN, to - from));
    if (span >= whole) {
      this.view = { ...limit };
    } else {
      const start = Math.max(limit.from, Math.min(from, limit.to - span));
      this.view = { from: start, to: start + span };
    }
    this.scheduleRender();
  }

  zoomBy(factor, anchor = (this.view.from + this.view.to) / 2) {
    const span = this.view.to - this.view.from;
    const next = span * factor;
    const ratio = (anchor - this.view.from) / span;
    this.setView(anchor - ratio * next, anchor + (1 - ratio) * next);
  }

  reset() {
    this.setView(this.bounds.from, this.bounds.to);
  }

  scheduleRender() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.render();
    });
  }

  // -- drawing -------------------------------------------------------------

  render() {
    const { from, to } = this.view;
    const { x0, x1, y0, y1 } = this.plot;
    const span = Math.max(1, to - from);
    const plotWidth = x1 - x0;
    const columns = Math.max(1, Math.min(MAX_COLUMNS, Math.round(plotWidth)));
    const x = (cycle) => x0 + ((cycle - from) / span) * plotWidth;

    setAttrs(this.svg, {
      width: this.width,
      height: this.height,
      viewBox: `0 0 ${this.width} ${this.height}`,
    });

    const stats = statsOver(this.timeline, this.powerAt, from, to);
    this.updateStats(from, to, span, stats);
    if (this.collapsed) {
      // Nothing to draw behind a collapsed card; redraw when it reopens.
      this.dirty = true;
      return;
    }
    const top = stats.peak > 0 ? stats.peak : 1;
    const y = (power) => y1 - (power / top) * (y1 - y0);

    const first = segmentAt(this.timeline, from);
    let visible = 0;
    for (let i = first; i < this.timeline.count; i += 1) {
      if (this.timeline.starts[i] >= to) break;
      visible += 1;
    }

    const dense = visible > columns * 1.5;
    const areas = STATES.map(() => '');
    const ribbons = STATES.map(() => '');
    const ribbonTop = y1 + RIBBON_GAP;
    const ribbonBottom = ribbonTop + RIBBON_HEIGHT;

    const bar = (left, right, state, power) => {
      const height = y1 - y(power);
      const width = Math.max(0.35, right - left);
      if (height > 0) {
        areas[state] += `M${left.toFixed(2)} ${y1}V${y(power).toFixed(2)}`
          + `h${width.toFixed(2)}V${y1}Z`;
      }
      ribbons[state] += `M${left.toFixed(2)} ${ribbonTop}h${width.toFixed(2)}`
        + `V${ribbonBottom}h${(-width).toFixed(2)}Z`;
    };

    if (dense) {
      this.forEachColumn(first, from, to, columns, (index, state, power) => {
        const left = x0 + (index / columns) * plotWidth;
        bar(left, left + plotWidth / columns + 0.3, state, power);
      });
    } else {
      for (let i = first; i < this.timeline.count; i += 1) {
        const start = this.timeline.starts[i];
        if (start >= to) break;
        const stop = Math.min(segmentEnd(this.timeline, i), to);
        if (stop <= from) continue;
        const state = this.timeline.states[i];
        bar(x(Math.max(start, from)), x(stop) + 0.25, state, this.powers[state]);
      }
    }

    this.areaPaths.forEach((path, index) => path.setAttribute('d', areas[index]));
    this.ribbonPaths.forEach((path, index) => path.setAttribute('d', ribbons[index]));

    setAttrs(this.baseAxis, { x1: x0, x2: x1, y1, y2: y1 });
    setAttrs(this.leftAxis, { x1: x0, x2: x0, y1: y0, y2: y1 });
    setAttrs(this.peakLine, { x1: x0, x2: x1, y1: y(top), y2: y(top) });
    this.peakLabel.setAttribute('y', y(top) + 3);
    this.peakLabel.textContent = tidy(top);
    this.zeroLabel.setAttribute('y', y1 + 3);

    const meanY = y(stats.mean);
    setAttrs(this.meanLine, { x1: x0, x2: x1, y1: meanY, y2: meanY });
    this.meanLine.setAttribute('opacity', stats.mean > 0 ? '0.85' : '0');

    this.renderMarkers(from, to, x);
    this.renderTicks(from, to, x, plotWidth);

    const notes = [];
    if (dense) {
      notes.push('More detail here than the chart has columns: each bar is its '
        + 'column\'s mean power, coloured by the state that dominated it. Zoom in '
        + 'for the exact steps.');
    }
    if (this.hiddenMarkers > 0) {
      notes.push(`${this.hiddenMarkers} more fireable marks are too close together `
        + 'to draw; zoom in to see them all.');
    }
    this.note.textContent = notes.join(' ');
    this.note.hidden = notes.length === 0;
    this.lastX = x;
    this.placePlayhead();
  }

  /** The row summary, which stays visible when the chart is collapsed. */
  updateStats(from, to, span, stats) {
    // The view pans and zooms continuously; cycles are integers, so report the
    // whole cycles the window actually covers.
    this.stats.textContent = `${cycleLabel(Math.round(from))}–`
      + `${cycleLabel(Math.max(Math.round(from), Math.ceil(to) - 1))}`
      + ` (${cycleLabel(Math.round(span))} cyc) · peak ${tidy(stats.peak)}`
      + ` · mean ${tidy(stats.mean)} · E ${tidy(stats.energy)}`;
  }

  /**
   * Bucket the visible segments into one bar per column. Scratch buffers are
   * reused, and only grown when the chart gets wider.
   */
  forEachColumn(first, from, to, columns, emit) {
    if (!this.colTotal || this.colTotal.length < columns) {
      this.colTotal = new Float64Array(columns);
      this.colEnergy = new Float64Array(columns);
      this.colHeld = new Float64Array(columns * STATES.length);
    }
    const { colTotal, colEnergy, colHeld, timeline } = this;
    const perColumn = (to - from) / columns;
    colTotal.fill(0, 0, columns);
    colEnergy.fill(0, 0, columns);
    colHeld.fill(0, 0, columns * STATES.length);

    for (let i = first; i < timeline.count; i += 1) {
      const start = timeline.starts[i];
      if (start >= to) break;
      const stop = Math.min(segmentEnd(timeline, i), to);
      if (stop <= from) continue;
      const state = timeline.states[i];
      const power = this.powers[state];
      const firstColumn = Math.max(0, Math.floor((Math.max(start, from) - from) / perColumn));
      const lastColumn = Math.min(columns - 1, Math.floor((stop - from) / perColumn));
      for (let column = firstColumn; column <= lastColumn; column += 1) {
        const t0 = from + column * perColumn;
        const overlap = Math.min(stop, t0 + perColumn) - Math.max(start, t0, from);
        if (overlap <= 0) continue;
        colTotal[column] += overlap;
        colEnergy[column] += power * overlap;
        colHeld[column * STATES.length + state] += overlap;
      }
    }

    for (let column = 0; column < columns; column += 1) {
      if (colTotal[column] <= 0) continue;
      let state = 0;
      let longest = -1;
      for (let s = 0; s < STATES.length; s += 1) {
        const held = colHeld[column * STATES.length + s];
        if (held > longest) {
          longest = held;
          state = s;
        }
      }
      emit(column, state, colEnergy[column] / colTotal[column]);
    }
  }

  /**
   * Arrows above the plot marking the cycles the actor became fireable -- the
   * moment work became available, which is not the moment it starts running:
   * the gap to the next execution block is what waking up cost.
   */
  renderMarkers(from, to, x) {
    const { y0 } = this.plot;
    this.hiddenMarkers = 0;
    if (!this.viewState.showFireable || this.fireable.length === 0) {
      this.markers.setAttribute('d', '');
      return;
    }
    const tip = y0 - 2;
    const tail = y0 - 9;
    let path = '';
    let lastPx = -Infinity;
    for (let i = firstAtLeast(this.fireable, from); i < this.fireable.length; i += 1) {
      const cycle = this.fireable[i];
      if (cycle >= to) break;
      const px = x(cycle);
      if (px - lastPx < MARKER_PITCH) {
        this.hiddenMarkers += 1;
        continue;
      }
      lastPx = px;
      path += `M${(px - 3.4).toFixed(2)} ${tail}H${(px + 3.4).toFixed(2)}`
        + `L${px.toFixed(2)} ${tip}Z`;
    }
    this.markers.setAttribute('d', path);
    this.markers.setAttribute('fill', this.colors.marker);
  }

  renderTicks(from, to, x, plotWidth) {
    const { colors } = this;
    const { y0, y1 } = this.plot;
    const nodes = [];
    let ticks = niceTicks(from, to, Math.max(5, Math.min(20, Math.round(plotWidth / 55))));
    // Cycle stamps grow as the numbers do; thin them out rather than overlap.
    const widest = ticks.reduce((most, cycle) => Math.max(most, cycleLabel(cycle).length), 1);
    const needed = widest * 6 + 12;
    while (ticks.length > 2 && plotWidth / ticks.length < needed) {
      ticks = ticks.filter((_, index) => index % 2 === 0);
    }
    ticks.forEach((cycle) => {
      const px = x(cycle);
      nodes.push(svgEl('line', {
        x1: px, x2: px, y1: y0, y2: y1,
        stroke: colors.grid, 'stroke-width': 0.5, opacity: 0.5,
      }));
      nodes.push(svgEl('line', {
        x1: px, x2: px, y1: y1 + 10, y2: y1 + 13,
        stroke: colors.axis, 'stroke-width': 1,
      }));
      const label = svgEl('text', {
        x: px, y: y1 + 22, 'text-anchor': 'middle', 'font-size': 9, fill: colors.faint,
      });
      label.textContent = cycleLabel(cycle);
      nodes.push(label);
    });
    this.gridGroup.replaceChildren(...nodes);
  }

  placePlayhead() {
    const cycle = this.playheadCycle;
    if (cycle === undefined || !this.lastX) return;
    const inside = cycle >= this.view.from && cycle < this.view.to;
    this.playhead.setAttribute('opacity', inside ? '1' : '0');
    if (!inside) return;
    // Sits on the cycle boundary, lining up with that cycle's stamp on the axis
    // and with the left edge of the bar it is pointing at.
    const px = this.lastX(cycle);
    const { y0, y1 } = this.plot;
    setAttrs(this.playhead, {
      x1: px, x2: px, y1: y0 - 3, y2: y1 + RIBBON_GAP + RIBBON_HEIGHT + 1,
    });
  }

  setPlayhead(cycle) {
    this.playheadCycle = cycle;
    this.placePlayhead();
  }
}

// --------------------------------------------------------------------------- //
// Panel
// --------------------------------------------------------------------------- //

export function renderPowerCharts(container, { actors, trace, graphName, viewState }) {
  const colors = {
    grid: css('--border-strong') || css('--border'),
    axis: css('--border-strong') || css('--border'),
    faint: css('--text-faint'),
    accent: css('--accent'),
    marker: css('--text-muted'),
    bg: css('--bg-elevated'),
    text: css('--text'),
    font: css('--font') || 'sans-serif',
  };

  const empty = () => {
    container.replaceChildren(el('p', {
      class: 'empty',
      text: 'Run a simulation to see each actor\'s power profile.',
    }));
    return { setPlayhead() {}, destroy() {} };
  };

  if (!trace || !trace.actors || Object.keys(trace.actors).length === 0) return empty();

  const bounds = {
    from: trace.first_cycle ?? 0,
    to: (trace.last_cycle ?? (trace.first_cycle ?? 0)) + 1,
  };

  const state = viewState || { collapsed: new Set(), heights: new Map() };
  if (state.showFireable === undefined) state.showFireable = true;
  const marks = trace.fireable || {};
  const charts = orderActors(actors, state.order).map((actor) => new PowerChart(
    actor, trace.actors[actor.id], bounds, colors, state, marks[actor.id],
  ));
  charts.forEach((chart) => {
    chart.onBroadcast = (view) => {
      charts.forEach((other) => {
        if (other !== chart) other.setView(view.from, view.to);
      });
    };
  });

  const parts = [el('p', {
    class: 'hint',
    text: `Cycles ${cycleLabel(bounds.from)}–${cycleLabel(bounds.to - 1)}. The area is `
      + 'shaded by the state the actor was in, and the arrows above each plot mark '
      + 'the cycles the actor became fireable -- the gap to the next execution '
      + 'block is what waking up cost. Scroll on a chart to zoom around the cursor, '
      + 'drag to pan, double-click to fit; the figures follow the visible range. '
      + 'S/M/L/XL sets a chart\'s height.',
  })];
  if (trace.truncated) {
    parts.push(el('p', {
      class: 'run-status is-error',
      text: 'The trace hit its event limit and stops early, so these charts do not '
        + 'cover the whole run. Narrow the trace window to see further in.',
    }));
  }
  const list = el('div', { class: 'power-list' });
  charts.forEach((chart) => list.append(chart.root));
  parts.push(list);
  const order = wireReordering(list, charts, state);

  const actions = el('div', { class: 'metric-actions' });
  const exportBtn = el('button', { class: 'btn', text: 'Export SVG' });
  exportBtn.addEventListener('click', () => {
    const blob = new Blob([exportSvg(order(), colors, graphName)], { type: 'image/svg+xml' });
    const url = URL.createObjectURL(blob);
    const anchor = Object.assign(document.createElement('a'), {
      href: url, download: `${graphName}-power.svg`,
    });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  });
  const resetBtn = el('button', { class: 'btn', text: 'Fit all' });
  resetBtn.addEventListener('click', () => charts.forEach((chart) => chart.reset()));
  const markBtn = el('button', { class: 'btn' });
  const syncMarks = () => {
    markBtn.textContent = state.showFireable ? 'Hide fireable marks' : 'Show fireable marks';
  };
  markBtn.addEventListener('click', () => {
    state.showFireable = !state.showFireable;
    syncMarks();
    charts.forEach((chart) => chart.scheduleRender());
  });
  syncMarks();

  const foldBtn = el('button', { class: 'btn' });
  const syncFold = () => {
    const anyOpen = charts.some((chart) => !chart.collapsed);
    foldBtn.textContent = anyOpen ? 'Collapse all' : 'Expand all';
  };
  foldBtn.addEventListener('click', () => {
    const collapse = charts.some((chart) => !chart.collapsed);
    charts.forEach((chart) => chart.setCollapsed(collapse));
    syncFold();
  });
  syncFold();
  charts.forEach((chart) => { chart.onFold = syncFold; });
  actions.append(markBtn, foldBtn, resetBtn, exportBtn);
  parts.push(actions);

  container.replaceChildren(...parts);

  // Charts are drawn at their real pixel size, so they redraw when the panel is
  // widened, maximised, or first shown (a hidden panel measures zero).
  const boxes = new Map(charts.map((chart) => [chart.plotBox, chart]));
  const observer = new ResizeObserver((entries) => {
    entries.forEach((entry) => {
      const chart = boxes.get(entry.target);
      if (chart) chart.setWidth(entry.contentRect.width);
    });
  });
  charts.forEach((chart) => {
    chart.setWidth(chart.plotBox.clientWidth);
    observer.observe(chart.plotBox);
  });

  return {
    setPlayhead(cycle) {
      charts.forEach((chart) => chart.setPlayhead(cycle));
    },
    destroy() {
      observer.disconnect();
      charts.forEach((chart) => chart.destroy());
      container.replaceChildren();
    },
  };
}

/**
 * Put actors in the order the user last dragged them into. Anything not in the
 * saved order -- a newly added actor -- keeps its natural place at the end.
 */
function orderActors(actors, saved) {
  if (!saved || saved.length === 0) return actors;
  const rank = new Map(saved.map((id, index) => [id, index]));
  return actors
    .map((actor, index) => ({
      actor,
      key: rank.has(actor.id) ? rank.get(actor.id) : saved.length + index,
    }))
    .sort((a, b) => a.key - b.key)
    .map((entry) => entry.actor);
}

/**
 * Drag-to-reorder by the grip handle, with arrow keys as the keyboard path.
 * The chart itself keeps its own drag gesture for panning, which is why the
 * handle is a separate control.
 */
function wireReordering(list, charts, state) {
  const byRoot = new Map(charts.map((chart) => [chart.root, chart]));
  const current = () => [...list.children].map((root) => byRoot.get(root));
  const remember = () => { state.order = current().map((chart) => chart.actor.id); };

  /** The card the pointer is currently above the top half of. */
  const dropTarget = (y, dragging) => {
    for (const root of list.children) {
      if (root === dragging) continue;
      const box = root.getBoundingClientRect();
      if (y < box.top + box.height / 2) return root;
    }
    return null;
  };

  let dragging = null;
  charts.forEach((chart) => {
    chart.grip.addEventListener('dragstart', (event) => {
      dragging = chart.root;
      chart.root.classList.add('is-dragging');
      event.dataTransfer.effectAllowed = 'move';
      // Firefox refuses to start a drag without payload.
      event.dataTransfer.setData('text/plain', chart.actor.id);
    });
    chart.grip.addEventListener('dragend', () => {
      if (dragging) dragging.classList.remove('is-dragging');
      dragging = null;
      remember();
    });
    chart.grip.addEventListener('keydown', (event) => {
      const step = event.key === 'ArrowUp' ? -1 : (event.key === 'ArrowDown' ? 1 : 0);
      if (!step) return;
      event.preventDefault();
      const roots = [...list.children];
      const index = roots.indexOf(chart.root);
      const target = index + step;
      if (target < 0 || target >= roots.length) return;
      if (step < 0) list.insertBefore(chart.root, roots[target]);
      else list.insertBefore(roots[target], chart.root);
      remember();
      chart.grip.focus();
    });
  });

  list.addEventListener('dragover', (event) => {
    if (!dragging) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const before = dropTarget(event.clientY, dragging);
    if (before === dragging) return;
    if (before) list.insertBefore(dragging, before);
    else list.append(dragging);
  });
  list.addEventListener('drop', (event) => event.preventDefault());

  return current;
}

/** Stack the live charts into one standalone, self-contained SVG file. */
function exportSvg(charts, colors, graphName) {
  const width = Math.max(FALLBACK_WIDTH, ...charts.map((chart) => chart.width));
  const rows = charts.map((chart) => chart.height + 26);
  const height = 44 + rows.reduce((total, row) => total + row, 0);
  const root = svgEl('svg', {
    xmlns: SVG_NS,
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    'font-family': colors.font,
  });
  root.append(svgEl('rect', { x: 0, y: 0, width, height, fill: colors.bg }));

  const title = svgEl('text', {
    x: 16, y: 26, 'font-size': 14, 'font-weight': 600, fill: colors.text,
  });
  title.textContent = `${graphName} — power per actor`;
  root.append(title);

  let top = 44;
  charts.forEach((chart, index) => {
    chart.ensureDrawn();
    const heading = svgEl('text', {
      x: 16, y: top + 12, 'font-size': 11, 'font-weight': 600, fill: colors.text,
    });
    heading.textContent = `${chart.actor.name} (${chart.actor.kind}) — `
      + chart.stats.textContent;
    root.append(heading);

    const clone = chart.svg.cloneNode(true);
    setAttrs(clone, {
      x: 0, y: top + 18, width: chart.width, height: chart.height,
    });
    clone.querySelectorAll('.power-playhead').forEach((node) => node.remove());
    root.append(clone);
    top += rows[index];
  });

  return `<?xml version="1.0" encoding="UTF-8"?>\n${root.outerHTML}`;
}
