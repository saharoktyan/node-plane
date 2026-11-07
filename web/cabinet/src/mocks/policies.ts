/** VPN limits and quotas on mocks: own values per installation, profile and
 * device, resolved as the controller does (device, profile, tariff,
 * installation, default), and a quota period per profile. */
import { http } from 'msw';
import type { Schemas } from '$lib/api/client';
import { error, guarded } from './people';
import { onTariff } from './profiles';
import { choice } from './scenario';

type Subject = 'installation' | 'profile' | 'device';
const KEYS: [string, Subject[], unknown][] = [
  ['devices.max', ['installation', 'profile'], null],
  ['connections.mode', ['installation', 'profile', 'device'], 'off'],
  ['connections.max_sources', ['installation', 'profile', 'device'], 1],
  ['connections.window_seconds', ['installation'], 120],
  ['connections.cooldown_seconds', ['installation'], 30],
  ['quota.bytes', ['installation', 'profile'], null],
  ['quota.reset', ['installation', 'profile'], 'calendar_month'],
  ['quota.interval_days', ['installation', 'profile'], 30],
  ['quota.on_exhausted', ['installation', 'profile'], 'freeze'],
  ['quota.throttle_kbit', ['installation', 'profile'], 1000],
  ['quota.timezone', ['installation'], 'UTC'],
  ['speed.up_kbit', ['installation', 'profile', 'device'], null],
  ['speed.down_kbit', ['installation', 'profile', 'device'], null],
  ['xray.bittorrent', ['installation'], 'allow']
];
// The tariff "Month" every second profile is on.
const TARIFF: Record<string, unknown> = { 'devices.max': 3, 'quota.bytes': 100 * 2 ** 30 };

type Own = Record<string, { value: unknown; revision: number }>;
let state: { scenario: string; own: Record<string, Own>; anchors: Record<string, { day: string; revision: number }>;
  resets: Record<string, string> } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  state = { scenario, anchors: {}, resets: {}, own: {
    installation: { 'connections.mode': { value: 'observe', revision: 2 }, 'speed.down_kbit': { value: 50_000, revision: 1 } },
    'p1-0000-4000-8000-000000000000': { 'quota.reset': { value: 'monthly_anchor', revision: 1 },
      'quota.on_exhausted': { value: 'throttle', revision: 1 } }
  } };
  return state;
}

/** The first own value along the chain; "tariff" stands for the profile's tariff. */
function resolve(key: string, chain: string[], tariff: boolean) {
  for (const id of chain) {
    if (id === 'tariff') {
      if (tariff && TARIFF[key] != null) return TARIFF[key];
      continue;
    }
    const own = store().own[id]?.[key];
    if (own) return own.value;
  }
  return KEYS.find(([name]) => name === key)![2];
}

function keys(subject: Subject, id: string, chain: string[]) {
  const tariff = subject === 'profile' ? onTariff(id) : subject === 'device' && onTariff(chain[1]);
  return KEYS.filter(([, subjects]) => subjects.includes(subject)).map(([key, , fallback]) => {
    const own = store().own[id]?.[key];
    return { key, value: own?.value ?? null, revision: own?.revision ?? 0, default: fallback,
      effective: resolve(key, chain, tariff),
      ...(subject === 'profile' ? { tariff: tariff ? (TARIFF[key] ?? null) : null } : {}) };
  });
}

async function patch(request: Request, id: string, subject: Subject) {
  const { changes } = (await request.json()) as Schemas['VpnPolicyInput'];
  const own = (store().own[id] ??= {});
  for (const change of changes) {
    if (!KEYS.some(([key, subjects]) => key === change.key && subjects.includes(subject))) return error(422, 'invalid_input');
    if ((own[change.key]?.revision ?? 0) !== change.revision) return error(409, 'revision_conflict');
  }
  for (const change of changes) {
    if (change.value == null) delete own[change.key];
    else own[change.key] = { value: change.value, revision: (own[change.key]?.revision ?? 0) + 1 };
  }
  return { changed: changes.length };
}

function quota(profile: string): Schemas['QuotaOutput'] {
  const chain = [profile, 'tariff', 'installation'];
  const tariff = onTariff(profile);
  const limit = resolve('quota.bytes', chain, tariff) as number | null;
  const reset = resolve('quota.reset', chain, tariff) as Schemas['QuotaOutput']['reset'];
  const anchor = store().anchors[profile] ?? { day: '2026-10-14', revision: 1 };
  const start = store().resets[profile] ?? '2026-11-01T00:00:00+00:00';
  const used = store().resets[profile] ? 0 : Math.round((limit ?? 0) * 0.62);
  const period = (id: string, from: string, to: string, gib: number, reason: string, exhausted = false) => ({
    id, start: from, end: to, closed_at: to, close_reason: reason, used_bytes: gib * 2 ** 30,
    uplink_bytes: gib * 2 ** 28, downlink_bytes: gib * 3 * 2 ** 28, blocked: false,
    exhausted_at: exhausted ? to : null, warned_at: null });
  return { limit_bytes: limit || null, reset, interval_days: resolve('quota.interval_days', chain, tariff) as number,
    on_exhausted: resolve('quota.on_exhausted', chain, tariff) as Schemas['QuotaOutput']['on_exhausted'],
    throttle_kbit: resolve('quota.throttle_kbit', chain, tariff) as number, throttled: null,
    timezone: resolve('quota.timezone', ['installation'], false) as string,
    anchor_at: anchor.day + 'T00:00:00+00:00', anchor_day: reset === 'monthly_anchor' ? anchor.day : null,
    anchor_revision: anchor.revision,
    period: limit ? { id: 'q-now', start, end: '2026-12-01T00:00:00+00:00', closed_at: null, close_reason: null,
      used_bytes: used, uplink_bytes: used / 4, downlink_bytes: (used * 3) / 4, blocked: false, exhausted_at: null,
      warned_at: null } : null,
    history: limit ? [period('q-10', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 100, 'period_end', true),
      period('q-09', '2026-09-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 41.5, 'period_end')] : [] };
}

export const policyHandlers = [
  http.get('/api/v1/vpn-policies/installation', guarded(() => ({ subject_kind: 'installation', subject_id: null,
    keys: keys('installation', 'installation', ['installation']), values: [] }))),
  http.patch('/api/v1/vpn-policies/installation', guarded((request) => patch(request, 'installation', 'installation'))),
  http.get('/api/v1/profiles/:id/vpn-policies', guarded((_, { id }) => ({ subject_kind: 'profile', subject_id: id,
    keys: keys('profile', id, [id, 'tariff', 'installation']), values: [] }))),
  http.patch('/api/v1/profiles/:id/vpn-policies', guarded((request, { id }) => patch(request, id, 'profile'))),
  http.get('/api/v1/profiles/:id/devices/:device/vpn-policies', guarded((_, { id, device }) => ({ subject_kind: 'device',
    subject_id: device, keys: keys('device', device, [device, id, 'tariff', 'installation']), values: [] }))),
  http.patch('/api/v1/profiles/:id/devices/:device/vpn-policies', guarded((request, { device }) => patch(request, device, 'device'))),
  http.get('/api/v1/profiles/:id/quota', guarded((_, { id }) => quota(id))),
  http.post('/api/v1/profiles/:id/quota/reset', guarded((_, { id }) => {
    store().resets[id] = new Date().toISOString();
    return quota(id);
  })),
  http.patch('/api/v1/profiles/:id/quota', guarded(async (request, { id }) => {
    const current = store().anchors[id] ?? { day: '2026-10-14', revision: 1 };
    if (request.headers.get('If-Match') !== `"${current.revision}"`) return error(412, 'revision_conflict');
    const { anchor_at } = (await request.json()) as Schemas['QuotaAnchorInput'];
    store().anchors[id] = { day: anchor_at.slice(0, 10), revision: current.revision + 1 };
    return quota(id);
  }))
];

// Temporary configurations: made in the background, active after a moment.
type Temporary = { id: string; node_key: string; node_title: string; protocol: string; transport: string;
  duration_seconds: number; status: string; created_at: string; expires_at: string | null; error_code: null;
  revocation_mode: string };
let temporary: { scenario: string; items: Temporary[] } | null = null;

function temporaries() {
  const { scenario } = choice();
  if (temporary?.scenario === scenario) return temporary;
  const at = (hours: number) => new Date(Date.now() + hours * 3_600_000).toISOString();
  temporary = { scenario, items: scenario === 'empty' ? [] : [
    { id: 't1000000-0000-4000-8000-000000000001', node_key: 'de1-0', node_title: 'Frankfurt 1', protocol: 'xray',
      transport: 'tcp', duration_seconds: 86_400, status: 'active', created_at: at(-5), expires_at: at(19), error_code: null,
      revocation_mode: 'new_connections_only' },
    { id: 't1000000-0000-4000-8000-000000000002', node_key: 'nl1-2', node_title: 'Amsterdam 3', protocol: 'awg',
      transport: 'conf', duration_seconds: 259_200, status: 'active', created_at: at(-30), expires_at: at(42), error_code: null,
      revocation_mode: 'peer_removed' }
  ] };
  return temporary;
}

function later(item: Temporary, status: string) {
  setTimeout(() => {
    if (status === 'gone') temporaries().items = temporaries().items.filter((entry) => entry !== item);
    else item.status = status;
  }, 3500);
}

export const temporaryHandlers = [
  http.get('/api/v1/system/temporary-configs', guarded(() => {
    const items = temporaries().items;
    return { items, total: items.length, page: 0, page_size: 100 };
  })),
  http.post('/api/v1/system/temporary-configs', guarded(async (request) => {
    const body = (await request.json()) as { node_key: string; protocol: string; transport: string; duration_seconds: number };
    const item: Temporary = { id: crypto.randomUUID(), ...body, node_title: body.node_key, status: 'queued',
      created_at: new Date().toISOString(), expires_at: new Date(Date.now() + body.duration_seconds * 1000).toISOString(),
      error_code: null, revocation_mode: body.protocol === 'awg' ? 'peer_removed' : 'new_connections_only' };
    temporaries().items.push(item);
    later(item, 'active');
    return item;
  })),
  http.get('/api/v1/system/temporary-configs/:id/artifact', guarded((_, { id }) => {
    const item = temporaries().items.find((entry) => entry.id === id);
    if (!item || item.status !== 'active') return error(503, 'config_unavailable');
    const name = `${item.node_title} · ${item.protocol === 'awg' ? 'AWG' : 'VLESS · ' + item.transport.toUpperCase()} · Temp`;
    if (item.protocol === 'xray') {
      const link = `vless://3f1c6a52-0000-4000-8000-${id.slice(0, 12)}@de1.example.net:443?type=${item.transport}&security=reality#${encodeURIComponent(name)}`;
      return { content: link, expires_at: item.expires_at, revocation_mode: item.revocation_mode,
        files: [{ filename: name + '.txt', content: link }] };
    }
    const key = 'vpn://' + btoa(`temporary ${id}`).replaceAll('=', '');
    const conf = `[Interface]\nPrivateKey = mock\nAddress = 10.8.0.42/32\n\n[Peer]\nEndpoint = nl1.example.net:51820\n`;
    return { content: item.transport === 'conf' ? conf : key, expires_at: item.expires_at, revocation_mode: item.revocation_mode,
      files: [{ filename: name + '.vpn', content: key }, { filename: name + '.conf', content: conf }] };
  })),
  http.post('/api/v1/system/temporary-configs/:id/revoke', guarded((_, { id }) => {
    const item = temporaries().items.find((entry) => entry.id === id);
    if (!item) return error(404, 'resource_not_found');
    item.status = 'revoking';
    later(item, 'gone');
    return item;
  }))
];
