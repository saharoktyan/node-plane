/** VPN as a service on mocks: tariffs with revisions, traffic counting,
 * the limits' delivery to servers, new server defaults and languages. */
import { http, HttpResponse } from 'msw';
import type { Schemas } from '$lib/api/client';
import { error, guarded } from './people';
import { localized } from './localized';
import { choice } from './scenario';

type Tariff = Schemas['TariffOutput'];
// The keys a tariff may set: what a profile may set, but the timezone.
const KEYS = ['devices.max', 'connections.mode', 'connections.max_sources', 'quota.bytes', 'quota.reset',
  'quota.interval_days', 'quota.on_exhausted', 'quota.throttle_kbit', 'speed.up_kbit', 'speed.down_kbit'];
const now = () => new Date().toISOString();

const shown = (value: Schemas['LocalizedTextOutput'] | null | undefined) =>
  value ? (value.shown[choice().lang] ?? value.texts[value.source] ?? '') : '';

let state: { scenario: string; tariffs: Tariff[]; traffic: { enabled: boolean; last_scan: string | null };
  defaults: { revision: number; protocols: string[]; xray_transports: string[]; settings: Record<string, unknown> } } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const empty = scenario === 'empty';
  const tariff = (id: string, en: string, ru: string, days: number | null, profiles: number, access: Tariff['access_policy'],
    policies: Record<string, unknown>): Tariff => {
    const titles = localized({ source: 'en', texts: { en, ru }, fallback: {} });
    return { id, title: shown(titles), titles, description: null, descriptions: null, duration_days: days, revision: 1,
      profiles, access_policy: access, policies, created_at: now(), updated_at: now() };
  };
  state = { scenario, traffic: { enabled: !empty, last_scan: empty ? null : new Date(Date.now() - 180_000).toISOString() },
    tariffs: empty ? [] : [
      tariff('t1', 'Month', 'Месяц', 30, 3, { explicit_grants: [], exclusions: [], rules: [{ scope: 'all', region_id: null,
        protocols: ['awg', 'xray'] }] }, { 'devices.max': 3, 'quota.bytes': 100 * 2 ** 30 }),
      tariff('t2', 'Europe, a year', 'Европа на год', 365, 0, { explicit_grants: [], exclusions: [], rules: [{ scope: 'region',
        region_id: 'europe', protocols: ['awg', 'xray'] }] }, {}),
      tariff('t3', 'Friends', 'Друзья', null, 1, null, { 'speed.down_kbit': 20_000 })
    ],
    defaults: { revision: 1, protocols: ['awg', 'xray'], xray_transports: ['tcp', 'xhttp'],
      settings: { awg_i1_preset: 'quic', awg_port_mode: 'auto', xray_sni: 'www.cloudflare.com', ipv6_outbound: 'on' } } };
  return state;
}

const find = (id: string) => store().tariffs.find((item) => item.id === id);
const revised = (request: Request, tariff: Tariff) => request.headers.get('If-Match') === `"${tariff.revision}"`;
const titled = (tariff: Tariff) => Object.assign(tariff, { title: shown(tariff.titles), description: shown(tariff.descriptions) || null });

// The servers with VPN and whether their limits are current; Tokyo cannot run speed limits.
function delivery(speed: boolean) {
  if (choice().scenario === 'empty') return { items: [] };
  return { items: [
    { node_key: 'de1-0', title: 'Frankfurt 1', flag: '🇩🇪', status: 'current', revision: 3, applied_revision: 3, checked_at: now() },
    { node_key: 'nl1-2', title: 'Amsterdam 3', flag: '🇳🇱', status: 'current', revision: 3, applied_revision: 3, checked_at: now() },
    { node_key: 'jp1-0', title: 'Tokyo', flag: '🇯🇵', status: speed ? 'failed' : 'pending', revision: 3, applied_revision: 2,
      checked_at: now(), protocols: speed ? [{ protocol: 'awg', state: 'unsupported' }] : [] }
  ] };
}

export const vpnAdminHandlers = [
  http.get('/api/v1/system/languages', guarded(() => ({ languages: ['en', 'ru'], default: 'en' }))),
  http.get('/api/v1/tariffs', guarded(() => ({ items: store().tariffs.map(titled), keys: KEYS }))),
  http.get('/api/v1/tariffs/:id', guarded((_, { id }) => (find(id) ? titled(find(id)!) : error(404, 'tariff_not_found')))),
  http.post('/api/v1/tariffs', guarded(async (request) => {
    const body = (await request.json()) as Schemas['TariffInput'];
    if (!body.titles) return error(422, 'invalid_input');
    const titles = localized(body.titles);
    if (store().tariffs.some((item) => item.titles?.texts[titles.source] === titles.texts[titles.source]))
      return error(409, 'tariff_exists');
    const tariff: Tariff = { id: crypto.randomUUID(), title: '', titles, description: null, descriptions: null, duration_days: null,
      revision: 1, profiles: 0, access_policy: null, policies: {}, created_at: now(), updated_at: now() };
    store().tariffs.push(tariff);
    return HttpResponse.json(titled(tariff), { status: 201 });
  })),
  http.patch('/api/v1/tariffs/:id', guarded(async (request, { id }) => {
    const tariff = find(id);
    if (!tariff) return error(404, 'tariff_not_found');
    if (!revised(request, tariff)) return error(409, 'revision_conflict');
    const body = (await request.json()) as Schemas['TariffInput'];
    if (body.titles) tariff.titles = localized(body.titles);
    if ('descriptions' in body) tariff.descriptions = body.descriptions ? localized(body.descriptions) : null;
    if ('duration_days' in body) tariff.duration_days = body.duration_days ?? null;
    if ('access_policy' in body) tariff.access_policy = (body.access_policy ?? null) as Tariff['access_policy'];
    if (body.policies) tariff.policies = body.policies;
    tariff.revision += 1;
    tariff.updated_at = now();
    return titled(tariff);
  })),
  http.delete('/api/v1/tariffs/:id', guarded((request, { id }) => {
    const tariff = find(id);
    if (!tariff) return error(404, 'tariff_not_found');
    if (!revised(request, tariff)) return error(409, 'revision_conflict');
    if (tariff.profiles) return error(409, 'tariff_in_use');
    store().tariffs = store().tariffs.filter((item) => item.id !== id);
    return { deleted: true };
  })),

  http.get('/api/v1/system/traffic', guarded(() => ({ ...store().traffic, collection_status: 'ready', interval_minutes: 5 }))),
  http.patch('/api/v1/system/traffic/preferences', guarded(async (request) => {
    const { enabled } = (await request.json()) as { enabled: boolean };
    // A new start forgets the last collection, as the controller does.
    store().traffic = { enabled, last_scan: null };
    return { ...store().traffic, collection_status: 'ready', interval_minutes: 5 };
  })),
  http.get('/api/v1/connection-limits/nodes', guarded(() => delivery(false))),
  http.get('/api/v1/speed-limits/nodes', guarded(() => delivery(true))),

  http.get('/api/v1/system/installation-defaults', guarded(() => store().defaults)),
  http.put('/api/v1/system/installation-defaults', guarded(async (request) => {
    const defaults = store().defaults;
    if (request.headers.get('If-Match') !== `"${defaults.revision}"`) return error(409, 'revision_conflict');
    const body = (await request.json()) as Schemas['InstallationDefaultsInput'];
    if (body.settings.awg_port_mode === 'manual' && body.settings.awg_port == null) return error(422, 'invalid_input');
    store().defaults = { revision: defaults.revision + 1, protocols: body.protocols, xray_transports: body.xray_transports,
      settings: body.settings as Record<string, unknown> };
    return store().defaults;
  }))
];
