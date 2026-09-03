// Thin wrapper over the Python backend. No simulation logic lives in the browser.

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && body.detail;
    if (detail && detail.problems) throw new ValidationFailure(detail.problems);
    throw new Error(typeof detail === 'string' ? detail : `request failed (${response.status})`);
  }
  return body;
}

export class ValidationFailure extends Error {
  constructor(problems) {
    super(problems.join('; '));
    this.problems = problems;
  }
}

const json = (payload) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});

export const api = {
  defaults: () => request('/api/defaults'),
  examples: () => request('/api/examples'),
  example: (name) => request(`/api/examples/${encodeURIComponent(name)}`),
  validate: (graph) => request('/api/validate', json({ graph })),
  simulate: (payload) => request('/api/simulate', json(payload)),
};
