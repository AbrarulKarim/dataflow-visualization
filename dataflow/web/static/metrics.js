// Metrics panel rendering and export.

import { STATES, STATE_COLORS } from './graph.js';

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  });
  children.forEach((child) => node.append(child));
  return node;
};

const compact = new Intl.NumberFormat(undefined, {
  notation: 'compact', maximumFractionDigits: 2,
});

function number(value, digits = 3) {
  if (!Number.isFinite(value)) return '—';
  if (value !== 0 && Math.abs(value) >= 100000) return compact.format(value);
  const text = value.toFixed(digits);
  // Trim trailing zeros only past a decimal point: "1660" must not become "166".
  const trimmed = text.includes('.')
    ? text.replace(/0+$/, '').replace(/\.$/, '')
    : text;
  return trimmed || '0';
}

function stat(label, value, unit) {
  const box = el('div', { class: 'stat' });
  const dd = el('dd', { text: value });
  if (unit) dd.append(el('small', { text: ` ${unit}` }));
  box.append(el('dt', { text: label }), dd);
  return box;
}

export function download(filename, content, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function toCSV(metrics) {
  const header = ['id', 'name', 'kind', 'firings', 'utilization', 'energy', 'avg_power',
    'tokens_in', 'tokens_out', ...STATES];
  const rows = metrics.actors.map((actor) => [
    actor.id, actor.name, actor.kind, actor.firings, actor.utilization,
    actor.energy, actor.avg_power, actor.tokens_in, actor.tokens_out,
    ...STATES.map((state) => actor.state_cycles[state]),
  ]);
  return [header, ...rows].map((row) => row.join(',')).join('\n');
}

export function renderMetrics(container, metrics, { graphName }) {
  const stats = el('dl', { class: 'stat-grid' });
  stats.append(
    stat('Throughput', number(metrics.throughput, 5), 'tok/cyc'),
    stat('Energy', number(metrics.energy, 1)),
    stat('Avg power', number(metrics.avg_power, 4)),
    stat('Tokens out', number(metrics.tokens_consumed, 0)),
  );

  const parts = [stats];

  if (metrics.deadlock_cycle !== null && metrics.deadlock_cycle !== undefined) {
    parts.push(el('p', {
      class: 'run-status is-error',
      text: `Deadlocked at cycle ${metrics.deadlock_cycle}: no actor could make progress. `
        + 'Figures still cover the full window.',
    }));
  }

  parts.push(el('p', {
    class: 'hint',
    text: `${metrics.cycles.toLocaleString()} cycles on the ${metrics.engine} engine in `
      + `${metrics.wall_time.toFixed(3)}s · source/sink energy ${number(metrics.special_energy, 1)} `
      + '(excluded above)',
  }));

  const list = el('div', { class: 'section' });
  list.append(el('span', { class: 'panel-label', text: 'Per actor' }));

  metrics.actors.forEach((actor) => {
    const row = el('div', { class: 'actor-metric' });
    const head = el('div', { class: 'actor-metric-head' });
    head.append(
      el('b', { text: actor.name }),
      el('span', { text: `E ${number(actor.energy, 1)}` }),
    );
    const sub = el('span', {
      class: 'actor-metric-sub',
      text: `${actor.kind_label || actor.kind} · ${actor.firings} firings · `
        + `${(actor.utilization * 100).toFixed(1)}% busy`,
    });
    const bar = el('div', {
      class: 'occupancy',
      title: STATES.map((state) => `${state}: ${actor.state_cycles[state]}`).join('\n'),
    });
    const total = STATES.reduce((sum, state) => sum + actor.state_cycles[state], 0) || 1;
    STATES.forEach((state) => {
      const share = (actor.state_cycles[state] / total) * 100;
      if (share > 0) {
        bar.append(el('i', {
          style: `width:${share}%;background:${STATE_COLORS[state]}`,
        }));
      }
    });
    row.append(head, sub, bar);
    list.append(row);
  });
  parts.push(list);

  const actions = el('div', { class: 'metric-actions' });
  const csvBtn = el('button', { class: 'btn', text: 'Export CSV' });
  csvBtn.addEventListener('click', () => download(`${graphName}-metrics.csv`, toCSV(metrics), 'text/csv'));
  const jsonBtn = el('button', { class: 'btn', text: 'Export JSON' });
  jsonBtn.addEventListener('click', () => download(`${graphName}-metrics.json`, JSON.stringify(metrics, null, 2)));
  actions.append(csvBtn, jsonBtn);
  parts.push(actions);

  container.replaceChildren(...parts);
}
