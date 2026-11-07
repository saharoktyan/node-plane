/** VPN profiles on mocks: the people's profiles and a couple without an
 * owner, kept for the tab so freezing, a term, a tariff or a renewal shows
 * on the next read. */
import { http, HttpResponse } from 'msw';
import type { Schemas } from '$lib/api/client';
import * as fixtures from './fixtures';
import { error, guarded, peopleAccounts } from './people';
import { choice } from './scenario';

type Profile = Schemas['ProfileOutput'];
type Policy = { explicit_grants: Schemas['GrantInput'][]; rules: Schemas['GrantPolicyRuleInput'][];
  exclusions: Schemas['GrantInput'][] };
type Entry = { profile: Profile; tariff: string | null; renewal: string | null; operation: Schemas['OperationOutput'];
  policy: Policy };
const now = () => new Date().toISOString();
const later = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString();

function operation(profile: Profile, status = 'succeeded', tasks: Schemas['OperationTaskOutput'][] = []) {
  return { id: crypto.randomUUID(), profile_id: profile.id, desired_revision: profile.desired_revision,
    created_at: now(), status, tasks };
}

const REGIONS = ['Europe', 'Asia', 'America', 'Oceania'].map((title) => ({ id: title.toLowerCase(), title }));

let state: { scenario: string; entries: Entry[] } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const owned = peopleAccounts('all').filter((item) => item.profile_id).map((item, index) => ({
    id: item.profile_id!, display_name: item.display_name ?? item.username ?? item.login ?? 'VPN',
    owner_account_id: item.id, frozen: index === 2, deleting: false, desired_revision: 3 + index,
    expires_at: item.role === 'admin' ? null : index === 1 ? later(17) : later(10 + index * 7)
  }));
  const loose: Profile[] = scenario === 'empty' ? [] : [
    { id: 'p9000000-0000-4000-8000-000000000001', display_name: 'Office router', owner_account_id: null,
      frozen: false, deleting: false, desired_revision: 2, expires_at: null },
    { id: 'p9000000-0000-4000-8000-000000000002', display_name: 'Guest · Kirill', owner_account_id: null,
      frozen: false, deleting: false, desired_revision: 1, expires_at: '2026-10-30T23:59:59+00:00' }
  ];
  const entries = [...owned, ...loose].map((profile, index) => ({
    profile, tariff: index % 2 === 1 ? 't1' : null, renewal: index === 1 ? crypto.randomUUID() : null,
    policy: index === 1
      ? { rules: [{ scope: 'region' as const, region_id: 'europe', protocols: ['awg' as const, 'xray' as const] }],
        explicit_grants: [{ node_key: 'us1-3', protocol: 'xray' }], exclusions: [{ node_key: 'de1-0', protocol: 'awg' }] }
      : { rules: [], explicit_grants: [{ node_key: 'de1-0', protocol: 'awg' }, { node_key: 'nl1-2', protocol: 'xray' }],
        exclusions: [] },
    operation: index === 2
      ? operation(profile, 'blocked', [{ id: crypto.randomUUID(), action: 'apply', node_key: 'de1', protocol: 'awg',
        status: 'blocked' }, { id: crypto.randomUUID(), action: 'apply', node_key: 'nl1', protocol: 'xray', status: 'succeeded' }])
      : operation(profile)
  }));
  state = { scenario, entries };
  return state;
}

const find = (id: string) => store().entries.find((entry) => entry.profile.id === id);

/** A change the nodes apply: running for a moment, then done. */
function applied(entry: Entry) {
  entry.profile.desired_revision += 1;
  entry.operation = operation(entry.profile, 'running', [{ id: crypto.randomUUID(), action: 'apply', node_key: 'de1',
    protocol: 'awg', status: 'running' }]);
  const current = entry.operation;
  setTimeout(() => {
    if (entry.operation === current) entry.operation = operation(entry.profile);
  }, 4000);
  return { profile: entry.profile, grants: [], operation_id: current.id, runtime_status: 'pending' };
}

function revised(request: Request, entry: Entry) {
  return request.headers.get('If-Match') === `"${entry.profile.desired_revision}"`;
}

export const profileHandlers = [
  http.get('/api/v1/profiles', guarded((request) => {
    const search = new URL(request.url).searchParams.get('search')?.toLowerCase() ?? '';
    return { items: store().entries.map((entry) => entry.profile)
      .filter((item) => item.display_name.toLowerCase().includes(search)), next_cursor: null };
  })),
  http.post('/api/v1/profiles', guarded(async (request) => {
    const body = (await request.json()) as Schemas['ProfileCreateInput'];
    const profile: Profile = { id: crypto.randomUUID(), display_name: body.display_name, owner_account_id: null,
      frozen: false, deleting: false, desired_revision: 1, expires_at: null };
    const entry = { profile, tariff: null, renewal: null, operation: operation(profile, 'no_targets'),
      policy: { rules: [], explicit_grants: [], exclusions: [] } };
    store().entries.unshift(entry);
    return HttpResponse.json({ profile, grants: [], operation_id: entry.operation.id, runtime_status: 'pending' },
      { status: 201 });
  })),
  // The member's own profile comes from the fixtures, as their pages read it.
  http.get('/api/v1/profiles/:id', guarded((_, { id }) => find(id)?.profile ?? fixtures.profile(id))),
  http.patch('/api/v1/profiles/:id', guarded(async (request, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    if (!revised(request, entry)) return error(409, 'revision_conflict');
    const body = (await request.json()) as Schemas['ProfileEditInput'];
    if (body.frozen != null) entry.profile.frozen = body.frozen;
    if ('expires_at' in body) entry.profile.expires_at = body.expires_at ?? null;
    if (body.display_name) entry.profile.display_name = body.display_name;
    return applied(entry);
  })),
  http.delete('/api/v1/profiles/:id', guarded((request, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    if (!revised(request, entry)) return error(409, 'revision_conflict');
    store().entries = store().entries.filter((item) => item !== entry);
    return { ...applied(entry), profile: { ...entry.profile, deleting: true } };
  })),
  http.get('/api/v1/profiles/:id/summary', guarded((_, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    const base = fixtures.summary(id, choice().lang);
    const { profile } = entry;
    return { ...base, display_name: profile.display_name, frozen: profile.frozen, expires_at: profile.expires_at,
      expired: profile.expires_at != null && new Date(profile.expires_at) < new Date(),
      tariff: entry.tariff ? base.tariff : null,
      renewal: entry.renewal ? { id: entry.renewal, status: 'pending', created_at: now() } : null,
      traffic: { status: 'current', month: '2026-11', items: [], nodes: ([['de1', 'awg', 7.2], ['nl1', 'xray', 2.4],
        ['de1', 'xray', 0.6], ['fi1', 'xray', 0.3]] as const).map(([node_key, protocol, gib]) => ({ node_key, protocol,
        status: 'current' as const, inbounds: {}, uplink_bytes: gib * 2 ** 28, downlink_bytes: gib * 3 * 2 ** 28 })) } };
  })),
  http.get('/api/v1/profiles/:id/operation', guarded((_, { id }) => find(id)?.operation ?? error(404, 'resource_not_found'))),
  http.get('/api/v1/profiles/:id/access-policy', guarded((_, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    const staff = peopleAccounts('all').find((item) => item.id === entry.profile.owner_account_id)?.role === 'admin';
    return { revision: entry.profile.desired_revision, permanent: staff, inherited_grants: [],
      ...(staff ? { rules: [{ scope: 'all', region_id: null, protocols: ['awg', 'xray'] }], explicit_grants: [], exclusions: [] }
        : entry.policy) };
  })),
  http.put('/api/v1/profiles/:id/access-policy', guarded(async (request, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    if (!revised(request, entry)) return error(409, 'revision_conflict');
    entry.policy = (await request.json()) as Policy;
    return applied(entry);
  })),
  http.get('/api/v1/regions', guarded(() => ({ items: REGIONS, next_cursor: null }))),
  http.put('/api/v1/profiles/:id/tariff', guarded(async (request, { id }) => {
    const entry = find(id);
    if (!entry) return error(404, 'resource_not_found');
    if (!revised(request, entry)) return error(409, 'revision_conflict');
    const body = (await request.json()) as Schemas['ProfileTariffInput'];
    entry.tariff = body.tariff_id ?? null;
    if (entry.tariff && body.expiry === 'start') entry.profile.expires_at = later(30);
    if (entry.tariff && body.expiry === 'extend') {
      const from = entry.profile.expires_at ? Math.max(Date.parse(entry.profile.expires_at), Date.now()) : Date.now();
      entry.profile.expires_at = new Date(from + 30 * 86_400_000).toISOString();
    }
    return applied(entry);
  })),
  http.post('/api/v1/renewal-requests/:id/approve', guarded(async (request, { id }) => {
    const entry = store().entries.find((item) => item.renewal === id);
    if (!entry) return error(409, 'renewal_decided');
    if (!revised(request, entry)) return error(409, 'revision_conflict');
    const { days } = (await request.json()) as { days: number | null };
    if (days == null && !entry.tariff) return error(422, 'renewal_days_required');
    const from = entry.profile.expires_at ? Math.max(Date.parse(entry.profile.expires_at), Date.now()) : Date.now();
    entry.profile.expires_at = new Date(from + (days ?? 30) * 86_400_000).toISOString();
    entry.renewal = null;
    applied(entry);
    return { id, status: 'approved', decided_at: now() };
  })),
  http.post('/api/v1/renewal-requests/:id/reject', guarded((_, { id }) => {
    const entry = store().entries.find((item) => item.renewal === id);
    if (!entry) return error(409, 'renewal_decided');
    entry.renewal = null;
    return { id, status: 'rejected', decided_at: now() };
  }))
];
