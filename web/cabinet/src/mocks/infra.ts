/** The relay, the web cabinet and the Telegram bot on mocks: each change is
 * applied after a couple of reads, and asks the session to confirm itself. */
import { http, HttpResponse } from 'msw';
import { unconfirmed } from './confirmation';
import { error, guarded } from './people';
import { choice } from './scenario';

const now = () => new Date().toISOString();
type Applying = { status: string; reads?: number };
let state: { scenario: string; relay: Record<string, unknown> & Applying | null; cabinet: Record<string, unknown> & Applying | null;
  bot: Record<string, unknown> & Applying | null; title: string } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const empty = scenario === 'empty';
  state = { scenario, title: 'Node Plane VPN', bot: null,
    relay: empty ? null : { node_key: 'de1-0', instance_id: 'r0', domain: 'cab.example.com', port: 443, tls: 'acme',
      acme_contact: 'admin@example.com', status: 'active', revision: 2, applied_revision: 2, relay_key: 'k', error_code: null,
      url: 'https://cab.example.com', updated_at: now(),
      link: { agent_reachable: true, running: true, connected: true, error: null, since: now() } },
    cabinet: empty ? null : { mode: 'relay', node_key: 'de1-0', listen: null, url: 'https://cab.example.com', public_url: null,
      status: 'active', state: 'active', revision: 1, applied_revision: 1, applied_version: '0.4.0', version: '0.4.0',
      error_code: null, updated_at: now() } };
  return state;
}

// A change in progress settles on the second read; a removal then goes away.
function settle<T extends Applying>(item: T | null): T | null {
  if (!item || !['applying', 'removing'].includes(item.status)) return item;
  item.reads = (item.reads ?? 0) + 1;
  if (item.reads < 2) return item;
  if (item.status === 'removing') return null;
  item.status = 'active';
  return item;
}
const output = <T extends Applying>(item: T | null) => {
  if (!item) return null;
  const { reads: _, ...rest } = item;
  return rest;
};

export const infraHandlers = [
  http.get('/api/v1/relay', guarded(() => {
    store().relay = settle(store().relay);
    return { relay: output(store().relay) };
  })),
  http.put('/api/v1/relay', guarded(async (request) => {
    const refused = unconfirmed();
    if (refused) return refused;
    const body = (await request.json()) as Record<string, unknown>;
    store().relay = { ...store().relay, ...body, instance_id: 'r0', status: 'applying', reads: 0, revision: 3, error_code: null,
      url: `https://${body.domain}`, updated_at: now(), link: { agent_reachable: true, running: true, connected: true } };
    return HttpResponse.json({ relay: output(store().relay) }, { status: 202 });
  })),
  http.delete('/api/v1/relay', guarded(() => {
    const refused = unconfirmed();
    if (refused) return refused;
    if (!store().relay) return error(404, 'resource_not_found');
    Object.assign(store().relay!, { status: 'removing', reads: 0 });
    return HttpResponse.json({ relay: output(store().relay) }, { status: 202 });
  })),

  http.get('/api/v1/web-cabinet', guarded(() => {
    store().cabinet = settle(store().cabinet);
    return { cabinet: output(store().cabinet), available: true, relay_node_key: store().relay ? 'de1-0' : null };
  })),
  http.put('/api/v1/web-cabinet', guarded(async (request) => {
    const refused = unconfirmed();
    if (refused) return refused;
    const body = (await request.json()) as { mode: string; listen?: string; public_url?: string };
    store().cabinet = { mode: body.mode, node_key: body.mode === 'relay' ? 'de1-0' : null, listen: body.listen ?? null,
      public_url: body.public_url ?? null, url: body.mode === 'relay' ? 'https://cab.example.com' : body.public_url,
      status: 'applying', reads: 0, state: 'applying', revision: 2, applied_revision: 1, applied_version: '0.4.0',
      version: '0.4.0', error_code: null, updated_at: now() };
    return HttpResponse.json({ cabinet: output(store().cabinet), available: true, relay_node_key: 'de1-0' }, { status: 202 });
  })),
  http.delete('/api/v1/web-cabinet', guarded(() => {
    const refused = unconfirmed();
    if (refused) return refused;
    if (store().cabinet) Object.assign(store().cabinet!, { status: 'removing', reads: 0 });
    return HttpResponse.json({ cabinet: output(store().cabinet), available: true, relay_node_key: 'de1-0' }, { status: 202 });
  })),

  http.get('/api/v1/telegram-bot/placement', guarded(() => {
    store().bot = settle(store().bot);
    return { placement: output(store().bot), telegram_enabled: true, relay_node_key: store().relay ? 'de1-0' : null };
  })),
  http.put('/api/v1/telegram-bot/placement', guarded(async (request) => {
    const refused = unconfirmed();
    if (refused) return refused;
    const { node_key } = (await request.json()) as { node_key: string };
    store().bot = { node_key, status: 'applying', reads: 0, state: 'applying', revision: 1, applied_revision: null,
      applied_version: null, version: '0.4.0', error_code: null, updated_at: now() };
    return HttpResponse.json({ placement: output(store().bot), telegram_enabled: true, relay_node_key: 'de1-0' }, { status: 202 });
  })),
  http.delete('/api/v1/telegram-bot/placement', guarded(() => {
    const refused = unconfirmed();
    if (refused) return refused;
    if (store().bot) Object.assign(store().bot!, { status: 'removing', reads: 0 });
    return HttpResponse.json({ placement: output(store().bot), telegram_enabled: true, relay_node_key: 'de1-0' }, { status: 202 });
  })),

  http.get('/api/v1/system/bot-title', guarded(() => ({ title: store().title }))),
  http.patch('/api/v1/system/bot-title', guarded(async (request) => {
    store().title = ((await request.json()) as { title: string }).title;
    return { title: store().title };
  }))
];
