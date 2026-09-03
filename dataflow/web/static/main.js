// Wiring: rail tools, canvas interaction, file actions, run config and transport.

import { api, ValidationFailure } from './api.js';
import { GraphEditor, STATES, STATE_COLORS, makeStyle, setKindLabels } from './graph.js';
import { Inspector } from './inspector.js';
import { download, renderMetrics } from './metrics.js';
import { Player } from './player.js';
import { renderPowerCharts } from './powerchart.js';

const $ = (id) => document.getElementById(id);
const workspace = document.querySelector('.workspace');
const canvasWrap = document.querySelector('.canvas-wrap');

let defaults = null;
let editor = null;
let inspector = null;
let player = null;
let armed = null;
let pendingSource = null;
let powerCharts = null;
// Collapsed rows and chart heights outlive a re-render, so a re-run keeps the
// layout the user set up.
const powerViewState = { collapsed: new Set(), heights: new Map() };
let mode = 'edit';
let toastTimer = null;

// ------------------------------------------------------------------ helpers

function toast(message, isError = false) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('is-error', isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 2600);
}

function parseCount(text, fallback = 0) {
  const value = Number(String(text).replace(/[_\s,]/g, ''));
  return Number.isFinite(value) && value >= 0 ? Math.round(value) : fallback;
}

function disarm() {
  armed = null;
  pendingSource = null;
  editor.cy.nodes().removeClass('is-pending');
  document.querySelectorAll('.tool').forEach((tool) => tool.classList.remove('is-armed'));
  canvasWrap.classList.remove('is-connecting');
  $('rail-hint').textContent = 'Pick an actor type, then click the canvas.';
}

function arm(button, kind) {
  const wasArmed = button.classList.contains('is-armed');
  disarm();
  if (wasArmed) return;
  button.classList.add('is-armed');
  if (kind === 'connect') {
    armed = { type: 'connect' };
    canvasWrap.classList.add('is-connecting');
    $('rail-hint').textContent = 'Click the producing actor, then the consuming actor.';
  } else {
    armed = { type: 'add', kind };
    const label = (defaults.actor_kinds.find((k) => k.value === kind) || {}).label || kind;
    $('rail-hint').textContent = `Click the canvas to place a ${label} actor. Esc to cancel.`;
  }
}

/**
 * Build the actor palette from `/api/defaults`, one sub-tab per kind group, so
 * the naming and grouping have a single home in the Python model.
 */
function buildPalette() {
  const groups = defaults.actor_groups;
  const tabs = $('actor-tabs');

  tabs.replaceChildren(...groups.map((group) => {
    const tab = document.createElement('button');
    tab.className = 'rail-tab';
    tab.textContent = group.label;
    tab.title = group.id === 'environment' ? 'Environment' : group.label;
    tab.dataset.group = group.id;
    tab.addEventListener('click', () => showActorGroup(group.id));
    return tab;
  }));

  groups.forEach((group) => {
    const panel = document.querySelector(`.actor-palette[data-group="${group.id}"]`);
    const kinds = defaults.actor_kinds.filter((kind) => kind.group === group.id);
    if (!kinds.length) {
      panel.replaceChildren(Object.assign(document.createElement('p'), {
        className: 'rail-note',
        textContent: 'Data-dependent firing rules (KPN, boolean dataflow, Dennis) '
          + 'will appear here.',
      }));
      return;
    }
    panel.replaceChildren(...kinds.map((kind) => {
      const button = document.createElement('button');
      button.className = 'tool';
      button.dataset.add = kind.value;
      button.append(
        Object.assign(document.createElement('b'), { textContent: kind.label }),
        Object.assign(document.createElement('small'), { textContent: kind.summary }),
      );
      button.addEventListener('click', () => arm(button, kind.value));
      return button;
    }));
  });

  showActorGroup(groups[0].id);
}

function showActorGroup(group) {
  document.querySelectorAll('.rail-tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.group === group);
  });
  document.querySelectorAll('.actor-palette').forEach((panel) => {
    panel.hidden = panel.dataset.group !== group;
  });
}

function markDirty() {
  if (player.available) {
    player.unload();
    $('transport').hidden = true;
    clearPower();
  }
}

// --------------------------------------------------------------------- mode

function setMode(next) {
  mode = next;
  const running = next === 'run';
  $('mode-edit').classList.toggle('is-active', !running);
  $('mode-run').classList.toggle('is-active', running);
  workspace.classList.toggle('is-running', running);
  editor.setTokenDisplay(running);
  editor.cy.autoungrabify(running);
  disarm();
  if (!running) setFullPage(false);  // building needs the canvas

  // The rail collapses or reappears, so the canvas changes size under us.
  requestAnimationFrame(() => { editor.cy.resize(); editor.fit(); });

  if (running) {
    showPanel(player.available ? 'metrics' : 'run');
    $('transport').hidden = !player.available;
    validateNow();
  } else {
    player.pause();
    $('transport').hidden = true;
    editor.resetStates();
    showPanel('inspector');
  }
}

function showPanel(name) {
  ['inspector', 'run', 'metrics', 'power'].forEach((panel) => {
    $(`panel-${panel}`).hidden = panel !== name;
  });
  document.querySelectorAll('.panel-tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.panel === name);
  });
  // Power profiles are time series; give them a wider column to live in.
  const wasWide = workspace.classList.contains('is-wide');
  workspace.classList.toggle('is-wide', name === 'power');
  if (wasWide !== (name === 'power')) {
    requestAnimationFrame(() => editor.cy.resize());
  }
}

function setFullPage(on) {
  const changed = workspace.classList.contains('is-fullpage') !== on;
  workspace.classList.toggle('is-fullpage', on);
  $('panel-max').setAttribute('aria-pressed', String(on));
  // The canvas is hidden in full page, so cytoscape needs its size back after.
  if (changed && !on) requestAnimationFrame(() => { editor.cy.resize(); editor.fit(); });
}

function renderPower() {
  // Tear the old charts down first: they hold wheel/pointer listeners and
  // per-chart scratch buffers.
  if (powerCharts) powerCharts.destroy();
  const actors = editor.cy.nodes()
    .map((node) => node.data('actor'))
    .sort((a, b) => a.id.localeCompare(b.id));
  powerCharts = renderPowerCharts($('panel-power'), {
    actors,
    trace: player.trace,
    graphName: editor.name,
    viewState: powerViewState,
  });
  powerCharts.setPlayhead(player.cycle);
}

function clearPower() {
  if (powerCharts) powerCharts.destroy();
  powerCharts = null;
  $('panel-power').replaceChildren(
    Object.assign(document.createElement('p'), {
      className: 'empty',
      textContent: "Run a simulation to see each actor's power profile.",
    }),
  );
}

async function validateNow() {
  const status = $('run-status');
  try {
    const { ok, problems } = await api.validate(editor.toJSON());
    if (ok) {
      status.className = 'run-status';
      status.textContent = 'Network is valid and ready to simulate.';
      return true;
    }
    status.className = 'run-status is-error';
    status.replaceChildren(document.createTextNode('This network cannot be simulated:'));
    const list = document.createElement('ul');
    problems.forEach((problem) => {
      const item = document.createElement('li');
      item.textContent = problem;
      list.append(item);
    });
    status.append(list);
    return false;
  } catch (error) {
    status.className = 'run-status is-error';
    status.textContent = error.message;
    return false;
  }
}

// ---------------------------------------------------------------- simulate

async function runSimulation() {
  const button = $('simulate-btn');
  const status = $('run-status');
  const traceEnd = $('cfg-trace-end').value.trim();
  const payload = {
    graph: editor.toJSON(),
    cycles: parseCount($('cfg-cycles').value, 1000),
    engine: $('cfg-engine').value,
    seed: parseCount($('cfg-seed').value, 0),
    trace: {
      enabled: true,
      start: parseCount($('cfg-trace-start').value, 0),
      end: traceEnd === '' ? null : parseCount(traceEnd, null),
    },
  };

  button.disabled = true;
  button.textContent = 'Simulating…';
  status.className = 'run-status';
  status.textContent = 'Running…';
  try {
    const result = await api.simulate(payload);
    player.load(result.trace);
    renderMetrics($('panel-metrics'), result.metrics, { graphName: editor.name });
    renderPower();
    $('transport').hidden = false;
    $('t-slider').min = String(player.first);
    $('t-slider').max = String(player.last);
    $('t-slider').value = String(player.first);
    $('t-total').textContent = `/ ${player.last}`;
    showPanel('metrics');
    status.textContent = `Done in ${result.metrics.wall_time.toFixed(3)}s.`;
    if (result.trace.truncated) {
      toast('Trace truncated at the event limit — narrow the trace window to replay more.');
    }
  } catch (error) {
    if (error instanceof ValidationFailure) {
      await validateNow();
    } else {
      status.className = 'run-status is-error';
      status.textContent = error.message;
    }
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Simulate';
  }
}

// -------------------------------------------------------------------- files

function newGraph() {
  editor.fromJSON({ version: 1, name: 'untitled', actors: [], channels: [] });
  $('graph-name').value = 'untitled';
  player.unload();
  $('transport').hidden = true;
  clearPower();
  inspector.clear();
  $('panel-metrics').replaceChildren(
    Object.assign(document.createElement('p'), {
      className: 'empty',
      textContent: 'Run a simulation to see throughput, power and energy.',
    }),
  );
  setMode('edit');
}

function loadGraph(graph) {
  editor.fromJSON(graph);
  $('graph-name').value = editor.name;
  inspector.clear();
  markDirty();
  editor.resetStates();
}

async function populateExamples() {
  const menu = $('examples-menu');
  try {
    const examples = await api.examples();
    menu.replaceChildren(...examples.map((example) => {
      const button = document.createElement('button');
      button.textContent = example.name;
      button.addEventListener('click', async () => {
        menu.hidden = true;
        loadGraph(await api.example(example.name));
        toast(`Loaded “${example.name}”.`);
      });
      return button;
    }));
    if (!examples.length) menu.textContent = 'No examples found.';
  } catch {
    menu.textContent = 'Could not load examples.';
  }
}

// ------------------------------------------------------------------ startup

function buildLegend() {
  $('legend').replaceChildren(...STATES.map((state) => {
    const span = document.createElement('span');
    const swatch = document.createElement('i');
    swatch.style.background = STATE_COLORS[state];
    span.append(swatch, document.createTextNode(state));
    return span;
  }));
}

function wireCanvas() {
  const cy = editor.cy;

  cy.on('tap', (event) => {
    if (event.target !== cy) return;
    if (mode === 'run' || !armed) {
      cy.elements().unselect();
      inspector.clear();
      return;
    }
    if (armed.type === 'add') {
      const node = editor.addActor(armed.kind, event.position, defaults);
      editor.refresh();
      markDirty();
      disarm();
      node.select();
      inspector.show(node);
      showPanel('inspector');
    }
  });

  cy.on('tap', 'node', (event) => {
    const node = event.target;
    if (mode === 'run') {
      inspector.show(node);
      return;
    }
    if (armed && armed.type === 'connect') {
      if (!pendingSource) {
        pendingSource = node;
        node.addClass('is-pending');
        $('rail-hint').textContent = `From ${node.data('actor').name} — now click the consumer.`;
        return;
      }
      const edge = editor.addChannel(pendingSource.id(), node.id());
      pendingSource.removeClass('is-pending');
      pendingSource = null;
      $('rail-hint').textContent = 'Click the producing actor, then the consuming actor.';
      editor.refresh();
      markDirty();
      edge.select();
      inspector.show(edge);
      showPanel('inspector');
      return;
    }
    inspector.show(node);
    showPanel('inspector');
  });

  cy.on('tap', 'edge', (event) => {
    if (armed) return;
    inspector.show(event.target);
    showPanel('inspector');
  });

  cy.on('dragfree', 'node', () => markDirty());
}

function wireChrome() {
  $('mode-edit').addEventListener('click', () => setMode('edit'));
  $('mode-run').addEventListener('click', () => setMode('run'));

  document.querySelectorAll('.panel-tab').forEach((tab) => {
    tab.addEventListener('click', () => showPanel(tab.dataset.panel));
  });

  $('tool-connect').addEventListener('click', () => arm($('tool-connect'), 'connect'));
  $('tool-delete').addEventListener('click', () => deleteSelection());
  $('tool-layout').addEventListener('click', () => editor.tidy());

  $('zoom-fit').addEventListener('click', () => editor.fit());
  $('zoom-in').addEventListener('click', () => editor.cy.zoom({
    level: editor.cy.zoom() * 1.25,
    renderedPosition: { x: editor.cy.width() / 2, y: editor.cy.height() / 2 },
  }));
  $('zoom-out').addEventListener('click', () => editor.cy.zoom({
    level: editor.cy.zoom() / 1.25,
    renderedPosition: { x: editor.cy.width() / 2, y: editor.cy.height() / 2 },
  }));

  $('graph-name').addEventListener('input', (event) => {
    editor.name = event.target.value.trim() || 'untitled';
  });

  $('new-btn').addEventListener('click', newGraph);
  $('save-btn').addEventListener('click', () => {
    download(`${editor.name}.dfg.json`, JSON.stringify(editor.toJSON(), null, 2));
  });
  $('open-btn').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      loadGraph(JSON.parse(await file.text()));
      toast(`Loaded ${file.name}.`);
    } catch (error) {
      toast(`Could not read ${file.name}: ${error.message}`, true);
    }
    event.target.value = '';
  });

  $('examples-btn').addEventListener('click', (event) => {
    event.stopPropagation();
    $('examples-menu').hidden = !$('examples-menu').hidden;
  });
  document.addEventListener('click', () => { $('examples-menu').hidden = true; });

  $('theme-btn').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    localStorage.setItem('dataflow-theme', next);
    editor.cy.style(makeStyle());
    editor.refresh();
    if (powerCharts) renderPower();
  });

  $('panel-max').addEventListener('click', () => {
    setFullPage(!workspace.classList.contains('is-fullpage'));
  });

  $('simulate-btn').addEventListener('click', runSimulation);

  // Transport
  $('t-play').addEventListener('click', () => player.toggle());
  $('t-back').addEventListener('click', () => player.step(-1));
  $('t-fwd').addEventListener('click', () => player.step(1));
  $('t-first').addEventListener('click', () => player.step(-Infinity));
  $('t-last').addEventListener('click', () => player.step(Infinity));
  $('t-slider').addEventListener('input', (event) => {
    // Read the target before pausing: pause() writes the current cycle back
    // into this very input.
    const target = Number(event.target.value);
    player.pause();
    player.seek(target);
  });
  $('t-speed').addEventListener('change', (event) => player.setSpeed(Number(event.target.value)));

  document.addEventListener('keydown', (event) => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName);
    if (event.key === 'Escape') {
      disarm();
      setFullPage(false);
      return;
    }
    if (typing) return;
    if (event.key === 'f' || event.key === 'F') { editor.fit(); return; }
    if ((event.key === 'Delete' || event.key === 'Backspace') && mode === 'edit') {
      event.preventDefault();
      deleteSelection();
    } else if (event.key === ' ' && player.available) {
      event.preventDefault();
      player.toggle();
    } else if (event.key === 'ArrowRight' && player.available) {
      player.step(event.shiftKey ? 10 : 1);
    } else if (event.key === 'ArrowLeft' && player.available) {
      player.step(event.shiftKey ? -10 : -1);
    }
  });
}

function deleteSelection() {
  const selected = editor.cy.$(':selected');
  if (selected.empty()) {
    toast('Nothing selected.');
    return;
  }
  selected.remove();
  inspector.clear();
  markDirty();
}

async function init() {
  const savedTheme = localStorage.getItem('dataflow-theme');
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;

  defaults = await api.defaults();
  editor = new GraphEditor($('cy'));
  player = new Player(editor, {
    onCycle: (cycle) => {
      $('t-cycle').textContent = String(cycle);
      $('t-slider').value = String(cycle);
      $('t-play').textContent = player.playing ? '❚❚' : '▶';
      if (powerCharts) powerCharts.setPlayhead(cycle);
    },
  });
  inspector = new Inspector($('panel-inspector'), {
    defaults,
    onChange: (element) => {
      editor.refresh(element);
      markDirty();
    },
    onDelete: (element) => {
      element.remove();
      inspector.clear();
      markDirty();
    },
  });

  setKindLabels(defaults.actor_kinds);
  buildLegend();
  buildPalette();
  wireCanvas();
  wireChrome();
  await populateExamples();

  try {
    const examples = await api.examples();
    if (examples.length) {
      loadGraph(await api.example(examples[0].name));
    }
  } catch {
    /* an empty canvas is a fine starting point */
  }
  editor.setTokenDisplay(false);
}

init().catch((error) => toast(`Startup failed: ${error.message}`, true));
