/** The installation's settings on mocks: the SSH key, Workstation's log,
 * and cleanup whose plan, job and steps ask the session to confirm itself. */
import { http } from 'msw';
import { unconfirmed } from './confirmation';
import { guarded } from './people';

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
const AUDIT = Array.from({ length: 23 }, (_, index) => ({ title: ['Узлы', 'Обновление', 'Резервная копия'][index % 3],
  occurred_at: ago(index * 47 + 5), account_label: 'admin', ssh_user: 'root', action: ['nodes.list', 'updates.run', 'backups.create'][index % 3],
  phase: index % 7 === 3 ? 'revoked' : 'completed', request_id: `req-${index}` }));
let job: { id: string; action: string; status: string; phase: string | null; error_code: null; can_abort: boolean;
  items: { node_key: string; status: string; error_code: null }[] } | null = null;

export const settingsHandlers = [
  http.get('/api/v1/system/ssh-key', guarded(() => ({ public_key:
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJm3xGq8mVb0e4wN1pYtK7rC2sLhQz5fUoE9aDiR6v node-plane-controller' }))),
  http.get('/api/v1/system/workstation-audit', guarded((request) => {
    const query = new URL(request.url).searchParams;
    const offset = Number(query.get('offset') ?? 0);
    const items = query.get('errors_only') === 'true' ? AUDIT.filter((item) => item.phase === 'revoked') : AUDIT;
    return { items: items.slice(offset, offset + 10), total: items.length, offset, page_size: 10 };
  })),
  http.get('/api/v1/system/cleanup', guarded(() => ({ supported: true, reason: null, counts: { accounts: 14, profiles: 12, nodes: 4 },
    latest_job: job }))),
  http.post('/api/v1/system/cleanup/plans', guarded(async (request) => {
    const body = (await request.json()) as { action: string; cleanup_nodes: boolean };
    return { id: crypto.randomUUID(), ...body, confirmation_phrase: body.action === 'reset' ? 'RESET NODE PLANE' : 'REMOVE NODE PLANE',
      nodes: [{ key: 'de1-0', title: 'Германия' }, { key: 'pl1', title: 'Польша' }] };
  })),
  http.post('/api/v1/system/cleanup/jobs', guarded(() => {
    const refused = unconfirmed();
    if (refused) return refused;
    job = { id: crypto.randomUUID(), action: 'reset', status: 'blocked', phase: 'nodes', error_code: null, can_abort: true,
      items: [{ node_key: 'de1-0', status: 'succeeded', error_code: null }, { node_key: 'pl1', status: 'failed', error_code: null }] };
    return job;
  })),
  http.post('/api/v1/system/cleanup/jobs/:id/:step', guarded((_, { step }) => {
    const refused = unconfirmed();
    if (refused) return refused;
    if (job) job = step === 'abort' ? null : { ...job, status: 'running', items: job.items.map((item) => ({ ...item, status: 'succeeded' })) };
    return job ?? {};
  })),
];
