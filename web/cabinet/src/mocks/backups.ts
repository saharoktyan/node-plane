/** Backups on mocks: a catalog, a job that ends after a few reads, the
 * schedule, and the dangerous ones asking the session to confirm itself. */
import { http, HttpResponse } from 'msw';
import { unconfirmed } from './confirmation';
import { error, guarded } from './people';
import { choice } from './scenario';

type Item = { id: string; created_at: string; app_version: string; trigger: string; kind: string; name: string; size_bytes: number };
type Job = { id: string; action: string; status: string; phase: string | null; result: unknown; reads: number };
const ago = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();
const CHECKSUM = 'a'.repeat(64);
let state: { scenario: string; items: Item[]; jobs: Job[]; policy: { enabled: boolean; interval_hours: number; keep_count: number } } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const item = (hours: number, kind: string, name = ''): Item => ({ id: crypto.randomUUID(), created_at: ago(hours),
    app_version: '0.4.0', trigger: kind === 'manual' ? 'manual' : 'scheduled', kind, name, size_bytes: 180_000 + hours * 900 });
  state = { scenario, jobs: [], policy: { enabled: true, interval_hours: 24, keep_count: 10 },
    items: scenario === 'empty' ? [] : [item(3, 'automatic'), item(27, 'manual', 'Перед обновлением'), item(51, 'automatic')] };
  return state;
}
const output = (job: Job) => {
  if (['awaiting_executor', 'running'].includes(job.status) && ++job.reads >= 3) {
    job.status = 'succeeded';
    if (job.action === 'create') store().items.unshift({ id: crypto.randomUUID(), created_at: new Date().toISOString(),
      app_version: '0.4.0', trigger: 'manual', kind: 'manual', name: '', size_bytes: 182_000 });
  } else if (job.status === 'awaiting_executor') job.status = 'running';
  const { reads: _, ...rest } = job;
  return rest;
};
const find = (id: string) => store().items.find((item) => item.id === id);

export const backupHandlers = [
  http.get('/api/v1/system/backups', guarded(() => {
    const { items, jobs, policy } = store();
    return { ...policy, count: items.length, size_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0),
      latest: items[0] ?? null, last_job: jobs[0] ? output(jobs[0]) : null };
  })),
  http.get('/api/v1/system/backups/catalog', guarded((request) => {
    const offset = Number(new URL(request.url).searchParams.get('offset') ?? 0);
    const { items } = store();
    return { items: items.slice(offset, offset + 8), total: items.length,
      total_size_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0), offset,
      next_offset: offset + 8 < items.length ? offset + 8 : null };
  })),
  http.get('/api/v1/system/backups/catalog/:id', guarded((_, { id }) => {
    const item = find(id);
    return item ? { ...item, checksum: CHECKSUM, compatible: true, accounts: 14, profiles: 12, nodes: 4 } : error(404, 'resource_not_found');
  })),
  http.get('/api/v1/system/backups/catalog/:id/export', guarded((_, { id }) =>
    find(id) ? new HttpResponse(JSON.stringify({ id }), { headers: { 'Content-Type': 'application/json' } }) : error(404, 'resource_not_found'))),
  http.post('/api/v1/system/backups/catalog/:id/delete', guarded((_, { id }) => {
    const refused = unconfirmed();
    if (refused) return refused;
    store().items = store().items.filter((item) => item.id !== id);
    return { deleted_backup_id: id };
  })),
  http.post('/api/v1/system/backups/import', guarded(() => {
    const refused = unconfirmed();
    if (refused) return refused;
    const id = crypto.randomUUID();
    store().items.unshift({ id, created_at: new Date().toISOString(), app_version: '0.4.0', trigger: 'manual', kind: 'manual',
      name: 'Imported', size_bytes: 150_000 });
    return HttpResponse.json({ status: 'success', backup_id: id }, { status: 201 });
  })),
  http.patch('/api/v1/system/backups/preferences', guarded(async (request) => {
    Object.assign(store().policy, await request.json());
    return store().policy;
  })),
  http.post('/api/v1/system/backups/jobs', guarded(async (request) => {
    const body = (await request.json()) as { action: string };
    if (body.action === 'restore') {
      const refused = unconfirmed();
      if (refused) return refused;
    }
    const job: Job = { id: crypto.randomUUID(), action: body.action, status: 'awaiting_executor', phase: null, result: null, reads: 0 };
    store().jobs.unshift(job);
    return HttpResponse.json(output(job), { status: 202 });
  })),
  http.get('/api/v1/system/backups/jobs/:id', guarded((_, { id }) => {
    const job = store().jobs.find((item) => item.id === id);
    return job ? output(job) : error(404, 'resource_not_found');
  }))
];
