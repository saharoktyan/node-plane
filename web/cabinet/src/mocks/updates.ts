/** Updates on mocks: a newer version waiting, the catalog with a rollback,
 * and a job that runs through its components and servers. */
import { http, HttpResponse } from 'msw';
import { unconfirmed } from './confirmation';
import { error, guarded } from './people';
import { choice } from './scenario';

type Job = { id: string; kind: string; status: string; created_at: string; target_ref: string; branch: string; reads: number;
  items: { node_key: string; status: string; error_code: string | null; title: string; flag: string }[]; result: unknown };
let state: { scenario: string; branch: string; auto: boolean; interval: number; checked: string; jobs: Job[]; dismissed: string[];
  current: string } | null = null;
function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  state = { scenario, branch: 'main', auto: true, interval: 360, checked: new Date(Date.now() - 3_600_000).toISOString(),
    jobs: [], dismissed: [], current: '0.4.0' };
  return state;
}
const available = () => store().current !== '0.4.1';
function output(job: Job) {
  if (['awaiting_executor', 'running'].includes(job.status)) {
    job.reads += 1;
    job.status = job.reads >= 4 ? 'partial' : 'running';
    const step = Math.min(job.reads, 4);
    job.result = { components: Object.fromEntries(['backend', 'worker', 'driver', 'telegram']
      .map((name, index) => [name, index < step ? 'succeeded' : index === step ? 'running' : 'waiting'])) };
    job.items = job.items.map((item, index) => ({ ...item, status: job.status === 'partial' ? (index === 2 ? 'failed' : 'succeeded')
      : 'running', error_code: job.status === 'partial' && index === 2 ? 'node_agent_unavailable' : null }));
    if (job.status === 'partial') store().current = job.target_ref.replace(/^v/, '');
  }
  const { reads: _, ...rest } = job;
  return rest;
}

export const updateHandlers = [
  http.get('/api/v1/system/updates', guarded(() => {
    const s = store();
    return { branch: s.branch, current_version: s.current, remote_version: '0.4.1', remote_label: 'v0.4.1',
      upstream_ref: 'v0.4.1', update_available: available(), update_supported: true, last_checked_at: s.checked,
      auto_check_enabled: s.auto, auto_check_interval_minutes: s.interval,
      latest_job: s.jobs[0] ? output(s.jobs[0]) : null, dismissed_job_ids: s.dismissed };
  })),
  http.get('/api/v1/system/updates/rollout', guarded(() => ({ driver_status: available() ? 'required' : 'current', nodes: [] }))),
  http.post('/api/v1/system/updates/check', guarded(() => {
    store().checked = new Date().toISOString();
    return {};
  })),
  http.patch('/api/v1/system/updates/preferences', guarded(async (request) => {
    const body = (await request.json()) as Record<string, unknown>;
    if ('auto_check_enabled' in body) store().auto = body.auto_check_enabled as boolean;
    if ('auto_check_interval_minutes' in body) store().interval = body.auto_check_interval_minutes as number;
    if ('branch' in body) store().branch = body.branch as string;
    return {};
  })),
  http.get('/api/v1/system/updates/versions', guarded(() => ({ status: 'ok', branch: store().branch, offset: 0, next_offset: null, total: 4,
    items: [
      { version: 'v0.4.1', ref: 'v0.4.1', action: available() ? 'upgrade' : 'current', allowed: available(), reason: available() ? 'upgrade' : 'current' },
      { version: 'v0.4.0', ref: 'v0.4.0', action: available() ? 'current' : 'downgrade', allowed: !available(), reason: available() ? 'current' : 'downgrade' },
      { version: 'v0.3.9', ref: 'v0.3.9', action: 'downgrade', allowed: false, reason: 'pre1_minor_downgrade_blocked' }
    ] }))),
  http.post('/api/v1/system/updates/run', guarded(async (request) => {
    const refused = unconfirmed();
    if (refused) return refused;
    const body = (await request.json()) as { kind: string; target_ref: string; branch: string };
    const job: Job = { id: crypto.randomUUID(), ...body, status: 'awaiting_executor', created_at: new Date().toISOString(), reads: 0,
      result: null, items: [['de1-0', 'Frankfurt 1', '🇩🇪'], ['nl1-2', 'Amsterdam 3', '🇳🇱'], ['jp1-0', 'Tokyo', '🇯🇵']]
        .map(([node_key, title, flag]) => ({ node_key, title, flag, status: 'waiting', error_code: null })) };
    store().jobs.unshift(job);
    return HttpResponse.json({ id: job.id, status: job.status }, { status: 202 });
  })),
  http.post('/api/v1/system/updates/jobs/:id/:action', guarded((_, { id, action }) => {
    const job = store().jobs.find((item) => item.id === id);
    if (!job) return error(404, 'resource_not_found');
    if (action === 'dismiss') store().dismissed.push(id);
    if (action === 'cancel') job.status = 'cancelled';
    return { id };
  }))
];
