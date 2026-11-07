/** The controller's API on mocks: enough for every page of the cabinet,
 * from the fixtures, following the scenario and role chosen. */
import { http, HttpResponse, delay } from 'msw';
import type { Schemas } from '$lib/api/client';
import * as fixtures from './fixtures';
import { peopleAccounts, peopleHandlers } from './people';
import { nodeHandlers } from './nodes';
import { policyHandlers, temporaryHandlers } from './policies';
import { profileHandlers } from './profiles';
import { vpnAdminHandlers } from './vpnadmin';
import { systemHandlers, unseenAlerts } from './system';
import { confirmationHandlers } from './confirmation';
import { infraHandlers } from './infra';
import { choice } from './scenario';

const ACCESS = 'mock-access';
const REFRESH = 'mock-refresh';

function tokens(): Schemas['SessionTokens'] {
  const later = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();
  return { session_id: 's1', access_token: ACCESS, access_expires_at: later(15), refresh_token: REFRESH,
    refresh_expires_at: later(43_200) };
}

function error(status: number, code: string) {
  return HttpResponse.json({ error: { code, message: code } }, { status });
}

/** Signed-in requests only; the errors scenario fails the reads of data. */
function guarded<T>(read: (request: Request) => T | Response, { fails = true } = {}) {
  return async ({ request }: { request: Request }) => {
    await delay(150);
    if (request.headers.get('Authorization') !== 'Bearer ' + ACCESS) return error(401, 'invalid_credentials');
    if (fails && choice().scenario === 'errors') return error(503, 'controller_unavailable');
    const value = read(request);
    return value instanceof Response ? value : HttpResponse.json(value as object);
  };
}

export const handlers = [
  http.post('/api/v1/sessions', async ({ request }) => {
    const body = (await request.json()) as Schemas['SignInInput'];
    await delay(200);
    return body.password === 'wrong' ? error(401, 'invalid_credentials') : HttpResponse.json(tokens(), { status: 201 });
  }),
  http.post('/api/v1/sessions/refresh', async ({ request }) => {
    const body = (await request.json()) as Schemas['RefreshInput'];
    return body.refresh_token === REFRESH ? HttpResponse.json(tokens()) : error(401, 'session_revoked');
  }),
  http.post('/api/v1/sessions/passkey/options', () => error(409, 'passkeys_unavailable')),
  http.get('/api/v1/registrations', () => HttpResponse.json({ open: true })),
  http.post('/api/v1/registrations/challenge', () =>
    HttpResponse.json({ challenge_id: 'c1', challenge: 'mock', bits: 8, expires_at: new Date().toISOString() })),
  http.post('/api/v1/registrations', () =>
    HttpResponse.json({ account: {}, session: tokens() }, { status: 201 })),
  http.post('/api/v1/invites/preview', async ({ request }) => {
    const body = (await request.json()) as Schemas['InviteTokenInput'];
    if (body.token === 'inv_used') return error(404, 'invite_invalid');
    const preview: Schemas['InvitePreview'] = { kind: 'new', vpn: true, tariff: choice().lang === 'ru' ? 'Месяц' : 'Month', login: null,
      has_password: false, expires_at: new Date(Date.now() + 86_400_000).toISOString() };
    return HttpResponse.json(preview);
  }),
  http.post('/api/v1/invites/accept', () => HttpResponse.json({ account: {}, session: tokens() })),

  http.get('/api/v1/me', guarded(() => {
    const { role, scenario, lang } = choice();
    return fixtures.me(role, scenario, lang);
  }, { fails: false })),
  http.patch('/api/v1/me/preferences', guarded(() => {
    const { role, scenario, lang } = choice();
    return fixtures.me(role, scenario, lang);
  })),
  http.put('/api/v1/me/password', guarded(() => {
    const { role, scenario, lang } = choice();
    return fixtures.me(role, scenario, lang);
  })),
  http.get('/api/v1/me/services', guarded(() => ({ items: fixtures.myServices(choice().scenario, choice().lang) }))),
  http.get('/api/v1/services', guarded(() => ({ items: fixtures.catalog(choice().role, choice().scenario) }))),
  http.post('/api/v1/me/access-requests', guarded(() => HttpResponse.json({}, { status: 201 }))),
  ...nodeHandlers,
  ...profileHandlers,
  ...policyHandlers,
  ...temporaryHandlers,
  ...vpnAdminHandlers,
  ...systemHandlers,
  ...confirmationHandlers,
  ...infraHandlers,
  http.get('/api/v1/me/profiles/:id/summary', guarded(({ url }) =>
    fixtures.summary(new URL(url).pathname.split('/')[5], choice().lang))),
  http.post('/api/v1/me/profiles/:id/renewal', guarded(() => HttpResponse.json({}, { status: 201 }))),
  http.get('/api/v1/profiles/:id', guarded(({ url }) => fixtures.profile(new URL(url).pathname.split('/')[4]))),
  http.get('/api/v1/profiles/:id/devices', guarded(() => fixtures.deviceList(choice().scenario))),
  http.post('/api/v1/profiles/:id/devices', guarded(() => HttpResponse.json({ device: fixtures.devices[0],
    profile_revision: 8, operation_id: null, runtime_status: 'ready' }, { status: 201 }))),
  http.patch('/api/v1/profiles/:id/devices/:device', guarded(() => ({ device: fixtures.devices[0],
    profile_revision: 8, operation_id: null, runtime_status: 'ready' }))),
  http.delete('/api/v1/profiles/:id/devices/:device', guarded(() => HttpResponse.json({ device: fixtures.devices[0],
    profile_revision: 8, operation_id: null, runtime_status: 'pending' }, { status: 202 }))),
  http.post('/api/v1/profiles/:id/devices/:device/rotate', guarded(() => HttpResponse.json({ device: fixtures.devices[0],
    profile_revision: 8, operation_id: null, runtime_status: 'pending' }, { status: 202 }))),
  http.get('/api/v1/profiles/:id/nodes', guarded(() => ({ items: fixtures.availableNodes, next_cursor: null }))),
  http.post('/api/v1/profiles/:id/config-issuances', async ({ request }) => {
    await delay(300);
    return HttpResponse.json(fixtures.issuance((await request.json()) as Schemas['ConfigIssuanceInput']),
      { status: 202 });
  }),
  http.get('/api/v1/config-issuances/:id/artifact', guarded(({ url }) =>
    fixtures.artifact(new URL(url).pathname.split('/')[4]))),
  http.get('/api/v1/me/notifications', guarded(() => ({ items: fixtures.inbox, total: fixtures.inbox.length,
    offset: 0, page_size: 10 }))),
  http.post('/api/v1/me/notifications/:id/dismiss', guarded(() => ({ dismissed: true }))),
  http.get('/api/v1/me/sessions', guarded(() => ({ items: fixtures.sessions }), { fails: false })),
  http.delete('/api/v1/me/sessions/:id', guarded(() => new HttpResponse(null, { status: 204 }), { fails: false })),
  http.get('/api/v1/me/passkeys', guarded(() => ({ items: fixtures.passkeys }))),
  http.get('/api/v1/me/workstations', guarded(() => ({ items: fixtures.workstations }))),
  http.delete('/api/v1/me/workstations/:id', guarded(() => new HttpResponse(null, { status: 204 }))),
  http.get('/api/v1/workstation-pairings/code/:code', guarded(() => fixtures.pairing)),
  http.post('/api/v1/workstation-pairings/code/:code/approve',
    guarded(() => ({ status: 'approved', name: fixtures.pairing.name }))),
  http.post('/api/v1/workstation-pairings/code/:code/reject',
    guarded(() => ({ status: 'rejected', name: fixtures.pairing.name }))),
  http.post('/api/v1/me/passkeys/options', guarded(() => error(409, 'passkeys_unavailable'))),
  http.delete('/api/v1/me/passkeys/:id', guarded(() => new HttpResponse(null, { status: 204 }))),

  http.get('/api/v1/admin/overview', guarded(() => fixtures.overview(choice().scenario))),
  http.get('/api/v1/admin/metrics/nodes', guarded((request) =>
    fixtures.nodeMetrics(choice().scenario, new URL(request.url).searchParams.get('range') ?? '24h'))),
  http.get('/api/v1/admin/metrics/vpn', guarded(() => fixtures.vpnActivity(choice().scenario))),
  http.get('/api/v1/me/profiles/:id/history', guarded(() => fixtures.trafficHistory(choice().scenario))),
  http.get('/api/v1/system/attention', guarded(() => ({ unacknowledged_alerts: unseenAlerts(),
    update_available: choice().scenario !== 'empty' }))),
  ...peopleHandlers,
  http.get('/api/v1/accounts', guarded(({ url }) => ({
    items: peopleAccounts(new URL(url).searchParams.get('access') ?? 'with'), next_cursor: null }))),
  http.get('/api/v1/nodes', guarded(() => ({ items: fixtures.nodes(choice().scenario), next_cursor: null })))
];
