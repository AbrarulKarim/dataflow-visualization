// Replays a change-only trace locally, so scrubbing and stepping are instant.

/** Value of a `[[cycle, value], ...]` series at an arbitrary cycle. */
function valueAt(series, cycle) {
  if (!series || series.length === 0) return undefined;
  if (cycle < series[0][0]) return series[0][1];
  let low = 0;
  let high = series.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (series[mid][0] <= cycle) low = mid;
    else high = mid - 1;
  }
  return series[low][1];
}

export class Player {
  constructor(editor, { onCycle }) {
    this.editor = editor;
    this.onCycle = onCycle;
    this.trace = null;
    this.cycle = 0;
    this.first = 0;
    this.last = 0;
    this.speed = 12;
    this.playing = false;
    this.rafId = null;
    this.lastFrame = 0;
    this.carry = 0;
  }

  load(trace) {
    this.trace = trace;
    this.first = trace.first_cycle ?? 0;
    this.last = Math.max(this.first, trace.last_cycle ?? 0);
    this.pause();
    this.seek(this.first);
  }

  /** Drop the trace: it no longer describes the network on the canvas. */
  unload() {
    this.pause();
    this.trace = null;
  }

  get available() {
    return Boolean(this.trace);
  }

  seek(cycle) {
    if (!this.trace) return;
    this.cycle = Math.min(this.last, Math.max(this.first, Math.round(cycle)));
    const { actors, channels } = this.trace;
    this.editor.cy.nodes().forEach((node) => {
      const state = valueAt(actors[node.id()], this.cycle);
      if (state !== undefined && node.data('state') !== state) node.data('state', state);
    });
    this.editor.cy.edges().forEach((edge) => {
      const tokens = valueAt(channels[edge.id()], this.cycle);
      if (tokens !== undefined && edge.data('tokens') !== tokens) edge.data('tokens', tokens);
    });
    this.editor.refresh();
    this.onCycle(this.cycle);
  }

  step(delta) {
    this.pause();
    this.seek(this.cycle + delta);
  }

  play() {
    if (!this.trace || this.playing) return;
    if (this.cycle >= this.last) this.seek(this.first);
    this.playing = true;
    this.lastFrame = performance.now();
    this.carry = 0;
    const frame = (now) => {
      if (!this.playing) return;
      const dt = (now - this.lastFrame) / 1000;
      this.lastFrame = now;
      this.carry += dt * this.speed;
      const advance = Math.floor(this.carry);
      if (advance > 0) {
        this.carry -= advance;
        const next = this.cycle + advance;
        if (next >= this.last) {
          this.seek(this.last);
          this.pause();
          return;
        }
        this.seek(next);
      }
      this.rafId = requestAnimationFrame(frame);
    };
    this.rafId = requestAnimationFrame(frame);
    this.onCycle(this.cycle);
  }

  pause() {
    this.playing = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    this.onCycle(this.cycle);
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  setSpeed(cyclesPerSecond) {
    this.speed = cyclesPerSecond;
  }
}
