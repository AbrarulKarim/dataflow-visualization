// Cytoscape is the single source of truth for the network: node data holds the
// actor record and edge data holds the channel record, both in the exact shape
// the Python .dfg.json schema uses.

export const STATES = ['idle', 'executing', 'shutdown', 'sleeping', 'wakeup'];

const css = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export const STATE_COLORS = Object.fromEntries(
  STATES.map((state) => [state, css(`--state-${state}`)]),
);

/**
 * Display names for actor kinds, filled from `/api/defaults` so the naming
 * lives in the Python model. Actor kinds are named for their rate behaviour --
 * SDF/HSDF/CSDF name whole models of computation, not individual actors.
 */
export const KIND_LABELS = {
  static_rate: 'Static rate',
  unit_rate: 'Unit rate',
  phased_rate: 'Phased rate',
  source: 'Source',
  sink: 'Sink',
};

export function setKindLabels(kinds) {
  kinds.forEach(({ value, label }) => { KIND_LABELS[value] = label; });
}

const SPECIAL = new Set(['source', 'sink']);
const rateText = (rate) => (Array.isArray(rate) ? rate.join(',') : String(rate));

export function makeStyle() {
  const ink = css('--state-ink');
  return [
    {
      selector: 'node',
      style: {
        shape: 'round-rectangle',
        // Sized from the label rather than cytoscape's deprecated 'label' value,
        // which also keeps every node card the same height.
        width: (ele) => {
          const actor = ele.data('actor');
          const name = actor.name || actor.id || '';
          const widest = Math.max(name.length, (KIND_LABELS[actor.kind] || '').length);
          return Math.min(150, Math.max(76, widest * 7 + 26));
        },
        height: 44,
        'background-color': (ele) => STATE_COLORS[ele.data('state')] || STATE_COLORS.idle,
        'border-width': 1.5,
        'border-color': (ele) =>
          SPECIAL.has(ele.data('actor').kind) ? css('--accent') : 'transparent',
        'border-style': (ele) => (SPECIAL.has(ele.data('actor').kind) ? 'dashed' : 'solid'),
        label: (ele) => {
          const actor = ele.data('actor');
          return `${actor.name || actor.id}\n${KIND_LABELS[actor.kind] || actor.kind}`;
        },
        'text-wrap': 'wrap',
        'text-max-width': '140px',
        'text-valign': 'center',
        'text-halign': 'center',
        'line-height': 1.35,
        color: ink,
        'font-family': css('--font') || 'sans-serif',
        'font-size': 11,
        'font-weight': 600,
        'transition-property': 'background-color, border-color, opacity',
        'transition-duration': '140ms',
        'overlay-opacity': 0,
      },
    },
    {
      selector: 'node:selected',
      style: { 'border-width': 2.5, 'border-color': css('--accent'), 'border-style': 'solid' },
    },
    {
      selector: 'node.is-pending',
      style: { 'border-width': 2.5, 'border-color': css('--accent'), 'border-style': 'double' },
    },
    {
      selector: 'edge',
      style: {
        'curve-style': 'bezier',
        width: 1.6,
        'line-color': css('--edge'),
        'target-arrow-color': css('--edge'),
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.9,
        label: 'data(label)',
        'font-family': css('--mono') || 'monospace',
        'font-size': 10,
        'font-weight': 600,
        color: css('--text'),
        'text-background-color': css('--edge-label-bg'),
        'text-background-opacity': 1,
        'text-background-padding': 3,
        'text-background-shape': 'roundrectangle',
        'text-border-color': css('--border'),
        'text-border-width': 1,
        'text-border-opacity': 1,
        'source-label': (ele) => rateText(ele.data('channel').production_rate),
        'target-label': (ele) => rateText(ele.data('channel').consumption_rate),
        'source-text-offset': 26,
        'target-text-offset': 26,
        'source-text-margin-y': -9,
        'target-text-margin-y': -9,
        'transition-property': 'line-color, width',
        'transition-duration': '140ms',
        'overlay-opacity': 0,
      },
    },
    {
      selector: 'edge:selected',
      style: { 'line-color': css('--accent'), 'target-arrow-color': css('--accent'), width: 2.6 },
    },
    {
      selector: 'edge.is-loaded',
      style: { width: 2.6, 'line-color': css('--accent'), 'target-arrow-color': css('--accent') },
    },
    { selector: '.is-dimmed', style: { opacity: 0.35 } },
  ];
}

export function defaultActor(kind, id, defaults) {
  const actor = {
    id,
    name: id,
    kind,
    phases: kind === 'phased_rate' ? 2 : 1,
    position: [0, 0],
    power: { ...defaults.power },
    timing: { ...defaults.timing },
    sleep_policy: SPECIAL.has(kind)
      ? { kind: 'never', timeout: 0 }
      : { ...defaults.sleep_policy },
  };
  if (kind === 'source') {
    actor.distribution = {
      kind: 'constant', mean: 4, stddev: 1, low: 1, high: 4, expression: '',
    };
  }
  return actor;
}

/**
 * Fill in what a `.dfg.json` may legitimately leave out. The Python loader
 * defaults a missing name to the actor id and every parameter block to the
 * model defaults, so a hand-written or scripted file must load here too.
 */
export function normaliseActor(raw, defaults) {
  const kind = raw.kind || 'static_rate';
  const position = Array.isArray(raw.position) && raw.position.length === 2
    ? raw.position
    : [0, 0];
  const actor = {
    id: raw.id,
    name: raw.name || raw.id,
    kind,
    phases: raw.phases ?? (kind === 'phased_rate' ? 2 : 1),
    position,
    power: { ...defaults.power, ...(raw.power || {}) },
    timing: { ...defaults.timing, ...(raw.timing || {}) },
    sleep_policy: SPECIAL.has(kind)
      ? { kind: 'never', timeout: 0 }
      : { ...defaults.sleep_policy, ...(raw.sleep_policy || {}) },
  };
  if (kind === 'source') {
    actor.distribution = {
      kind: 'constant', mean: 4, stddev: 1, low: 1, high: 4, expression: '',
      ...(raw.distribution || {}),
    };
  }
  return actor;
}

export function normaliseChannel(raw) {
  return {
    id: raw.id,
    name: raw.name || '',
    src: raw.src,
    dst: raw.dst,
    capacity: raw.capacity === undefined ? null : raw.capacity,
    initial_tokens: raw.initial_tokens ?? 0,
    production_rate: raw.production_rate ?? 1,
    consumption_rate: raw.consumption_rate ?? 1,
  };
}

export function defaultChannel(id, src, dst) {
  return {
    id,
    name: '',
    src,
    dst,
    capacity: 8,
    initial_tokens: 0,
    production_rate: 1,
    consumption_rate: 1,
  };
}

export class GraphEditor {
  constructor(container, defaults) {
    this.defaults = defaults;
    this.cy = cytoscape({
      container,
      style: makeStyle(),
      minZoom: 0.2,
      maxZoom: 2.5,
      selectionType: 'single',
      boxSelectionEnabled: false,
    });
    this.name = 'untitled';
    this.counter = 0;
    this.showTokens = false;
  }

  nextId(prefix) {
    let id;
    do {
      this.counter += 1;
      id = `${prefix}${this.counter}`;
    } while (this.cy.getElementById(id).nonempty());
    return id;
  }

  addActor(kind, position, defaults) {
    const prefix = kind === 'source' ? 'src' : (kind === 'sink' ? 'snk' : 'a');
    const actor = defaultActor(kind, this.nextId(prefix), defaults);
    const node = this.cy.add({
      group: 'nodes',
      data: { id: actor.id, actor, state: 'idle' },
      position: { x: position.x, y: position.y },
    });
    return node;
  }

  addChannel(srcId, dstId) {
    const channel = defaultChannel(this.nextId('ch'), srcId, dstId);
    return this.cy.add({
      group: 'edges',
      data: {
        id: channel.id,
        source: srcId,
        target: dstId,
        channel,
        label: '',
        tokens: channel.initial_tokens,
      },
    });
  }

  /** Refresh derived presentation for one element (or all of them). */
  refresh(element) {
    const targets = element ? [element] : this.cy.elements().toArray();
    targets.forEach((ele) => {
      if (ele.isEdge()) {
        const channel = ele.data('channel');
        const tokens = ele.data('tokens') ?? channel.initial_tokens;
        const capacity = channel.capacity == null ? '∞' : channel.capacity;
        ele.data('label', this.showTokens ? `${tokens}` : `⌀ ${capacity}`);
        ele.toggleClass('is-loaded', this.showTokens && tokens > 0);
      }
    });
    this.cy.style().update();
  }

  setTokenDisplay(on) {
    this.showTokens = on;
    this.refresh();
  }

  resetStates() {
    this.cy.nodes().forEach((node) => node.data('state', 'idle'));
    this.cy.edges().forEach((edge) => edge.data('tokens', edge.data('channel').initial_tokens));
    this.refresh();
  }

  // -- serialisation -------------------------------------------------------

  toJSON() {
    return {
      version: 1,
      name: this.name,
      actors: this.cy.nodes().map((node) => {
        const actor = structuredClone(node.data('actor'));
        const { x, y } = node.position();
        actor.position = [Math.round(x), Math.round(y)];
        return actor;
      }),
      channels: this.cy.edges().map((edge) => structuredClone(edge.data('channel'))),
    };
  }

  fromJSON(graph) {
    this.cy.elements().remove();
    this.name = graph.name || 'untitled';
    this.counter = 0;
    const elements = [];
    (graph.actors || []).forEach((raw, index) => {
      const actor = normaliseActor(raw, this.defaults);
      const [x, y] = actor.position[0] || actor.position[1]
        ? actor.position
        : [140 + index * 190, 220];
      elements.push({
        group: 'nodes',
        data: { id: actor.id, actor, state: 'idle' },
        position: { x, y },
      });
    });
    (graph.channels || []).forEach((raw) => {
      const channel = normaliseChannel(raw);
      elements.push({
        group: 'edges',
        data: {
          id: channel.id,
          source: channel.src,
          target: channel.dst,
          channel,
          label: '',
          tokens: channel.initial_tokens,
        },
      });
    });
    this.cy.add(elements);
    this.counter = 0;  // nextId() skips ids already taken by the loaded graph
    this.refresh();
    this.fit();
  }

  fit() {
    if (this.cy.elements().nonempty()) this.cy.fit(this.cy.elements(), 60);
  }

  /** Dependency-free layered layout: columns follow the longest path from a source. */
  tidy() {
    const nodes = this.cy.nodes();
    if (nodes.empty()) return;
    const depth = new Map(nodes.map((node) => [node.id(), 0]));
    // Relaxation over |V| passes settles the longest path even with cycles.
    for (let pass = 0; pass < nodes.length; pass += 1) {
      let changed = false;
      this.cy.edges().forEach((edge) => {
        const from = depth.get(edge.source().id());
        const to = depth.get(edge.target().id());
        if (from + 1 > to && from + 1 < nodes.length) {
          depth.set(edge.target().id(), from + 1);
          changed = true;
        }
      });
      if (!changed) break;
    }
    const columns = new Map();
    nodes.forEach((node) => {
      const level = depth.get(node.id());
      if (!columns.has(level)) columns.set(level, []);
      columns.get(level).push(node);
    });
    [...columns.entries()].forEach(([level, members]) => {
      members.forEach((node, index) => {
        node.position({
          x: 140 + level * 210,
          y: 200 + (index - (members.length - 1) / 2) * 130,
        });
      });
    });
    this.fit();
  }
}
