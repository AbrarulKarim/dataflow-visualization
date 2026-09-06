// Parameter forms for the selected actor or channel.

import { KIND_LABELS } from './graph.js';

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

function field(label, input, { wide = false } = {}) {
  const wrapper = el('label', { class: `field${wide ? ' field-wide' : ''}` });
  wrapper.append(el('span', { text: label }), input);
  return wrapper;
}

function numberInput(value, onInput, { step = 'any', min = null } = {}) {
  const input = el('input', { type: 'number', step, value: String(value) });
  if (min !== null) input.setAttribute('min', String(min));
  input.addEventListener('input', () => {
    const parsed = Number(input.value);
    if (input.value !== '' && Number.isFinite(parsed)) onInput(parsed);
  });
  return input;
}

function textInput(value, onInput, placeholder = '') {
  const input = el('input', { type: 'text', value, placeholder });
  input.addEventListener('input', () => onInput(input.value));
  return input;
}

function selectInput(options, value, onInput) {
  const select = el('select');
  options.forEach(([optValue, optLabel]) => {
    const option = el('option', { value: optValue, text: optLabel });
    if (optValue === value) option.selected = true;
    select.append(option);
  });
  select.addEventListener('change', () => onInput(select.value));
  return select;
}

function section(title, fields, { grid = true } = {}) {
  const wrapper = el('div', { class: 'section' });
  wrapper.append(el('span', { class: 'panel-label', text: title }));
  const body = el('div', { class: grid ? 'field-grid' : '' });
  fields.filter(Boolean).forEach((f) => body.append(f));
  wrapper.append(body);
  return wrapper;
}

const parseRate = (text) => {
  const parts = text.split(',').map((piece) => piece.trim()).filter(Boolean);
  const numbers = parts.map((piece) => Math.max(0, Math.round(Number(piece) || 0)));
  return numbers.length > 1 ? numbers : (numbers[0] ?? 1);
};

const rateText = (rate) => (Array.isArray(rate) ? rate.join(', ') : String(rate));

export class Inspector {
  constructor(container, { defaults, onChange, onDelete }) {
    this.container = container;
    this.defaults = defaults;
    this.onChange = onChange;
    this.onDelete = onDelete;
    this.element = null;
  }

  clear() {
    this.element = null;
    this.container.replaceChildren(
      el('p', { class: 'empty', text: 'Select an actor or channel to edit its parameters.' }),
    );
  }

  show(element) {
    this.element = element;
    if (!element) return this.clear();
    return element.isNode() ? this.showActor(element) : this.showChannel(element);
  }

  touch() {
    if (this.element) this.onChange(this.element);
  }

  head(title, kindLabel) {
    const header = el('div', { class: 'inspector-head' });
    header.append(el('h2', { text: title }), el('span', { class: 'pill', text: kindLabel }));
    return header;
  }

  deleteButton(label) {
    const button = el('button', { class: 'btn btn-danger btn-block', text: label });
    button.addEventListener('click', () => this.onDelete(this.element));
    return button;
  }

  showActor(node) {
    const actor = node.data('actor');
    const special = actor.kind === 'source' || actor.kind === 'sink';
    const parts = [this.head(actor.name, KIND_LABELS[actor.kind] || actor.kind)];

    parts.push(section('Identity', [
      field('Name', textInput(actor.name, (value) => {
        actor.name = value || actor.id;
        this.touch();
      }), { wide: true }),
      field('Type', selectInput(
        this.defaults.actor_kinds.map((kind) => [kind.value, kind.label]),
        actor.kind,
        (value) => {
          actor.kind = value;
          if (value === 'phased_rate' && actor.phases < 2) actor.phases = 2;
          if (value !== 'phased_rate') actor.phases = 1;
          if (value === 'source' && !actor.distribution) {
            actor.distribution = {
              kind: 'constant', mean: 4, stddev: 1, low: 1, high: 4, expression: '',
            };
          }
          if (value === 'source' || value === 'sink') {
            actor.sleep_policy = { kind: 'never', timeout: 0 };
          }
          this.touch();
          this.showActor(node);
        },
      )),
      actor.kind === 'phased_rate'
        ? field('Phases', numberInput(actor.phases, (value) => {
          actor.phases = Math.max(1, Math.round(value));
          this.touch();
        }, { step: 1, min: 1 }))
        : null,
    ]));

    if (actor.kind === 'source') {
      const dist = actor.distribution;
      const kindField = field('Arrivals', selectInput(
        this.defaults.distributions.map((k) => [k, k]),
        dist.kind,
        (value) => { dist.kind = value; this.touch(); this.showActor(node); },
      ));
      const params = [];
      if (dist.kind !== 'uniform' && dist.kind !== 'custom') {
        params.push(field('Mean interval', numberInput(dist.mean, (value) => {
          dist.mean = value; this.touch();
        }, { min: 0 })));
      }
      if (dist.kind === 'gaussian') {
        params.push(field('Std dev', numberInput(dist.stddev, (value) => {
          dist.stddev = value; this.touch();
        }, { min: 0 })));
      }
      if (dist.kind === 'uniform') {
        params.push(
          field('Min', numberInput(dist.low, (value) => { dist.low = value; this.touch(); })),
          field('Max', numberInput(dist.high, (value) => { dist.high = value; this.touch(); })),
        );
      }
      if (dist.kind === 'custom') {
        params.push(field('Expression', textInput(dist.expression, (value) => {
          dist.expression = value; this.touch();
        }, 'e.g. 5 + 3 * rng.random()'), { wide: true }));
      }
      parts.push(section('Token generation', [kindField, ...params]));
    }

    parts.push(section('Timing (cycles)', [
      field('Execution', numberInput(actor.timing.exec_time, (value) => {
        actor.timing.exec_time = Math.max(1, Math.round(value));
        this.touch();
      }, { step: 1, min: 1 })),
      special ? null : field('Sleep delay', numberInput(actor.timing.sleep_delay, (value) => {
        actor.timing.sleep_delay = Math.max(0, Math.round(value));
        this.touch();
      }, { step: 1, min: 0 })),
      special ? null : field('Wakeup delay', numberInput(actor.timing.wakeup_delay, (value) => {
        actor.timing.wakeup_delay = Math.max(0, Math.round(value));
        this.touch();
      }, { step: 1, min: 0 })),
    ]));

    const powerFields = [
      ['exec_power', 'Executing'],
      ['idle_power', 'Idle'],
      ...(special ? [] : [
        ['sleep_power', 'Sleeping'],
        ['shutdown_power', 'Shutdown'],
        ['wakeup_power', 'Wakeup'],
      ]),
    ].map(([key, label]) => field(label, numberInput(actor.power[key], (value) => {
      actor.power[key] = value;
      this.touch();
    }, { min: 0 })));
    parts.push(section('Power', powerFields));

    if (special) {
      parts.push(el('p', {
        class: 'hint',
        text: `${KIND_LABELS[actor.kind]} actors never sleep and are excluded from the `
          + 'global power and energy totals.',
      }));
    } else {
      const policy = actor.sleep_policy;
      const policyFields = [
        field('Policy', selectInput(
          this.defaults.sleep_kinds.map((k) => [k, k]),
          policy.kind,
          (value) => { policy.kind = value; this.touch(); this.showActor(node); },
        ), { wide: policy.kind === 'never' || policy.kind === 'immediate' }),
      ];

      if (policy.kind === 'timeout') {
        policyFields.push(field('Idle timeout', numberInput(policy.timeout, (value) => {
          policy.timeout = Math.max(0, Math.round(value));
          this.touch();
        }, { step: 1, min: 0 })));
      }

      if (policy.kind === 'adaptive') {
        policyFields.push(field('Strategy', selectInput(
          this.defaults.adaptive_strategies.map((s) => [s.value, s.label]),
          policy.adaptive_strategy,
          (value) => { policy.adaptive_strategy = value; this.touch(); this.showActor(node); },
        ), { wide: true }));

        if (policy.adaptive_strategy === 'weighted_moving_average') {
          policyFields.push(
            field('X (numerator)', numberInput(policy.wma_factor, (value) => {
              policy.wma_factor = Math.max(0, value);
              this.touch();
            }, { min: 0 })),
          );
        }

        // N bounds how much fireability history is remembered at all, so it
        // applies to every adaptive strategy, not just the built-in formula.
        policyFields.push(field('N (window)', numberInput(policy.wma_window, (value) => {
          policy.wma_window = Math.max(1, Math.round(value));
          this.touch();
        }, { step: 1, min: 1 })));

        if (policy.adaptive_strategy === 'custom') {
          policyFields.push(field('Formula', textInput(policy.custom_expression, (value) => {
            policy.custom_expression = value;
            this.touch();
          }, 'e.g. wakeup_delay + mean_gap / 2'), { wide: true }));
        }

        policyFields.push(field('Bootstrap timeout', numberInput(policy.timeout, (value) => {
          policy.timeout = Math.max(0, Math.round(value));
          this.touch();
        }, { step: 1, min: 0 }), { wide: true }));
      }

      parts.push(section('Sleep strategy', policyFields));

      if (policy.kind === 'adaptive' && policy.adaptive_strategy === 'weighted_moving_average') {
        parts.push(el('p', {
          class: 'hint',
          text: 'Sleep delay = X × execution time / (average of the last N fireability '
            + 'gaps): frequent arrivals keep the actor awake longer, rare ones send it '
            + 'to sleep sooner. Bootstrap timeout applies until N gaps have been '
            + 'observed.',
        }));
      }
      if (policy.kind === 'adaptive' && policy.adaptive_strategy === 'custom') {
        parts.push(el('p', {
          class: 'hint',
          text: 'A Python expression for the sleep delay, evaluated fresh each time '
            + 'it\'s needed. In scope: exec_time, sleep_delay, wakeup_delay, timeout '
            + '(the bootstrap value), window (N), gaps (the last up-to-N fireability '
            + 'gaps, oldest first — gaps[-1] is the most recent) and mean_gap '
            + '(their average). Also available: len, sum, max, min, abs, round, math. '
            + 'Bootstrap timeout applies until there is at least one gap; if the '
            + 'formula itself fails (e.g. it indexes further back than N once history '
            + 'has not filled that far yet), it falls back to the bootstrap timeout '
            + 'for that decision only.',
        }));
      }
      parts.push(el('p', {
        class: 'hint',
        text: 'A shutdown cannot be interrupted: work arriving mid-transition still pays '
          + 'the full sleep and wakeup delay.',
      }));
    }

    parts.push(this.deleteButton('Delete actor'));
    this.container.replaceChildren(...parts);
  }

  showChannel(edge) {
    const channel = edge.data('channel');
    const parts = [this.head(channel.id, 'channel')];

    const endpoints = el('p', { class: 'hint' });
    endpoints.append(
      el('b', { text: edge.source().data('actor').name }),
      document.createTextNode('  →  '),
      el('b', { text: edge.target().data('actor').name }),
    );
    parts.push(endpoints);
    this.container.replaceChildren(...parts);

    const capacityInput = el('input', {
      type: 'text',
      value: channel.capacity == null ? '' : String(channel.capacity),
      placeholder: 'unbounded',
    });
    capacityInput.addEventListener('input', () => {
      const raw = capacityInput.value.trim();
      channel.capacity = raw === '' ? null : Math.max(1, Math.round(Number(raw) || 1));
      this.touch();
    });

    this.container.append(section('FIFO', [
      field('Capacity', capacityInput),
      field('Initial tokens', numberInput(channel.initial_tokens, (value) => {
        channel.initial_tokens = Math.max(0, Math.round(value));
        edge.data('tokens', channel.initial_tokens);
        this.touch();
      }, { step: 1, min: 0 })),
    ]));

    this.container.append(section('Rates', [
      field('Produced', textInput(rateText(channel.production_rate), (value) => {
        channel.production_rate = parseRate(value);
        this.touch();
      }, '1 or 1,2,0')),
      field('Consumed', textInput(rateText(channel.consumption_rate), (value) => {
        channel.consumption_rate = parseRate(value);
        this.touch();
      }, '1 or 1,2,0')),
    ]));

    this.container.append(el('p', {
      class: 'hint',
      text: 'Comma-separated rates define a cyclo-static pattern, one entry per phase '
        + 'of the actor at that end.',
    }));
    this.container.append(this.deleteButton('Delete channel'));
  }
}
