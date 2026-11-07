/** People on mocks: accounts, requests, invites, registration and merges,
 * kept for the tab so a decision or a change shows on the next read. */
import { http, HttpResponse, delay, type PathParams } from 'msw';
import type { Schemas } from '$lib/api/client';
import * as fixtures from './fixtures';
import { choice } from './scenario';
import { localized, textIn } from './localized';

type Account = Schemas['AdminAccountOutput'];
const now = () => new Date().toISOString();
const later = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString();

export function error(status: number, code: string) {
  return HttpResponse.json({ error: { code, message: code } }, { status });
}

let state: {
  scenario: string;
  accounts: Account[];
  requests: Schemas['AccessRequestOutput'][];
  invites: Schemas['InviteOutput'][];
  merges: Schemas['MergeRequestOutput'][];
  policy: Schemas['AccessRequestPolicyOutput'];
  registration: Schemas['RegistrationSettings'];
  bot: Schemas['TelegramBotUsername'];
} | null = null;

// The bot's messages as the controller starts with them, every language for an editor.
function policy(): Schemas['AccessRequestPolicyOutput'] {
  const gate = localized({ source: 'en', fallback: {}, texts: { en: 'Authorization is required to use this bot.',
    ru: 'Чтобы пользоваться ботом, нужна авторизация.' } });
  const welcome = localized({ source: 'en', fallback: {}, texts: { en: 'Request access to use this bot.',
    ru: 'Запросите доступ для использования бота.' } });
  const lang = choice().lang;
  return { enabled: true, notify_requests: true, telegram: true, gate_message: textIn(gate, lang), welcome_message: textIn(welcome, lang),
    gate_messages: gate, welcome_messages: welcome };
}

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const empty = scenario === 'empty';
  const accounts = fixtures.accounts(scenario, 'all').map((item, index) => ({
    ...item, sessions: index === 0 ? 2 : index % 3, passkeys: index === 0 ? 1 : 0, workstations: index === 0 ? 1 : 0,
    profile_id: index % 3 === 2 ? null : `p${index}-0000-4000-8000-000000000000`,
    status: index === 3 ? 'disabled' : item.status
  }));
  state = {
    scenario, accounts,
    requests: empty ? [] : [
      { id: 'r1000000-0000-4000-8000-000000000001', account_id: null, status: 'pending', service: 'vpn',
        telegram_user_id: 5550001, username: 'anna_k', first_name: 'Anna', last_name: 'K.', display_name: null,
        language_code: 'ru', locale: 'ru', created_at: '2026-11-06T09:12:00+00:00', decided_at: null, decided_by: null },
      { id: 'r1000000-0000-4000-8000-000000000002', account_id: null, status: 'pending', service: 'vpn',
        telegram_user_id: 5550002, username: null, first_name: 'Boris', last_name: null, display_name: null,
        language_code: 'en', locale: null, created_at: '2026-11-07T07:40:00+00:00', decided_at: null, decided_by: null }
    ],
    invites: empty ? [] : [
      { id: 'i1000000-0000-4000-8000-000000000001', kind: 'new', vpn: true, tariff_id: 't1', account_id: null,
        note: 'For the design team', status: 'active', created_at: '2026-11-05T10:00:00+00:00',
        created_by: accounts[0]?.id ?? 'a', expires_at: later(5), used_at: null, used_account_id: null },
      { id: 'i1000000-0000-4000-8000-000000000002', kind: 'new', vpn: false, tariff_id: null, account_id: null,
        note: '', status: 'used', created_at: '2026-10-20T10:00:00+00:00', created_by: accounts[0]?.id ?? 'a',
        expires_at: '2026-10-27T10:00:00+00:00', used_at: '2026-10-21T10:00:00+00:00', used_account_id: accounts[1]?.id ?? null }
    ],
    merges: empty ? [] : [{
      id: 'm1000000-0000-4000-8000-000000000001', main_account_id: accounts[1]?.id ?? 'a', other_account_id: accounts[2]?.id ?? 'b',
      status: 'pending', keep_profile: null, created_at: '2026-11-06T15:00:00+00:00', decided_at: null,
      preview: {
        main: { id: accounts[1]?.id, login: 'user1', display_name: 'User 1', username: 'user1', telegram_user_id: 101,
          role: 'member', has_password: true, passkeys: 0, vpn: { devices: 2, tariff: 'Month', expires_at: '2026-12-06T00:00:00+00:00' } },
        other: { id: accounts[2]?.id, login: 'user2', display_name: 'User 2', username: null, telegram_user_id: null,
          role: 'member', has_password: true, passkeys: 1, vpn: { devices: 1, tariff: null, expires_at: '2026-11-30T00:00:00+00:00' } },
        result: { vpn: 'choose', telegram: null, password: 'dropped', login: 'dropped', passkeys_removed: 1, keep_main_only: false }
      }
    }],
    policy: policy(),
    registration: { open: false, cleanup_days: null },
    bot: { telegram_enabled: true, username: 'node_plane_vpn_bot' }
  };
  return state;
}

/** Signed-in requests only, as the other mocks. */
export function guarded<T>(read: (request: Request, params: Record<string, string>) => Promise<T | Response> | T | Response) {
  return async ({ request, params }: { request: Request; params: PathParams }) => {
    await delay(120);
    if (request.headers.get('Authorization') !== 'Bearer mock-access') return error(401, 'invalid_credentials');
    if (choice().scenario === 'errors' && request.method === 'GET') return error(503, 'controller_unavailable');
    const value = await read(request, params as Record<string, string>);
    return value instanceof Response ? value : HttpResponse.json(value as object);
  };
}

function find(id: string) {
  return store().accounts.find((item) => item.id === id);
}

export const peopleHandlers = [
  http.get('/api/v1/accounts/:id', guarded((_, { id }) => find(id) ?? error(404, 'resource_not_found'))),
  http.patch('/api/v1/accounts/:id', guarded(async (request, { id }) => {
    const person = find(id);
    if (!person) return error(404, 'resource_not_found');
    if (request.headers.get('If-Match') !== `"${person.revision}"`) return error(409, 'revision_conflict');
    const body = (await request.json()) as Schemas['AccountEditInput'];
    if (body.login === 'admin') return error(422, 'login_reserved');
    if (body.roles && person.roles.includes('owner') && !body.roles.includes('owner')
      && store().accounts.filter((item) => item.roles.includes('owner')).length === 1) return error(409, 'last_owner_required');
    Object.assign(person, {
      ...(body.login ? { login: body.login } : {}), ...(body.status ? { status: body.status } : {}),
      ...(body.roles ? { roles: body.roles, role: body.roles.length ? 'admin' : 'member' } : {}),
      revision: person.revision + 1
    });
    return person;
  })),
  http.delete('/api/v1/accounts/:id/sessions', guarded((_, { id }) => Object.assign(find(id) ?? {}, { sessions: 0 }))),
  http.delete('/api/v1/accounts/:id/passkeys', guarded((_, { id }) => Object.assign(find(id) ?? {}, { passkeys: 0 }))),
  http.delete('/api/v1/accounts/:id/workstations', guarded((_, { id }) => {
    Object.assign(find(id) ?? {}, { workstations: 0 });
    return { unlinked: 1 };
  })),
  http.post('/api/v1/accounts', guarded(async (request) => {
    const body = (await request.json()) as Schemas['AccountProvisionInput'];
    const id = crypto.randomUUID();
    const telegram = body.identity && /^\d+$/.test(body.identity) ? Number(body.identity) : null;
    store().accounts.unshift({ id, role: 'member', roles: [], status: 'approved', revision: 1, telegram_user_id: telegram,
      username: body.identity?.startsWith('@') ? body.identity.slice(1) : null, login: null,
      display_name: body.display_name ?? null, account_created: true, deleted: false, has_password: false,
      locale_selected: false, passkeys: 0, sessions: 0, workstations: 0, profile_id: crypto.randomUUID() });
    return HttpResponse.json({ account: { id }, profile: {}, created: true }, { status: 201 });
  })),

  http.get('/api/v1/access-requests', guarded(() => {
    const items = store().requests;
    return { items, next_cursor: null, pending_total: items.filter((item) => item.status === 'pending').length };
  })),
  http.post('/api/v1/access-requests/:id/decision', guarded(async (request, { id }) => {
    const item = store().requests.find((entry) => entry.id === id);
    if (!item) return error(404, 'resource_not_found');
    const { decision } = (await request.json()) as Schemas['AccessDecisionInput'];
    store().requests = store().requests.filter((entry) => entry.id !== id);
    return { ...item, status: decision === 'approve' ? 'approved' : 'rejected', decided_at: now() };
  })),
  http.get('/api/v1/system/access-requests', guarded(() => store().policy)),
  http.patch('/api/v1/system/access-requests', guarded(async (request) => {
    const body = (await request.json()) as Schemas['AccessRequestPolicyInput'];
    const policy = store().policy;
    if (body.enabled != null) policy.enabled = body.enabled;
    if (body.notify_requests != null) policy.notify_requests = body.notify_requests;
    for (const name of ['welcome', 'gate'] as const) {
      const input = body[`${name}_messages`];
      if (!input) continue;
      const value = localized(input);
      policy[`${name}_messages`] = value;
      policy[`${name}_message`] = textIn(value, choice().lang);
    }
    return policy;
  })),

  http.get('/api/v1/invites', guarded(() => ({ items: store().invites }))),
  http.post('/api/v1/invites', guarded(async (request) => {
    const body = (await request.json()) as Schemas['InviteInput'];
    const token = 'inv_' + crypto.randomUUID().replaceAll('-', '').slice(0, 24);
    const invite: Schemas['InviteOutput'] = { id: crypto.randomUUID(), kind: body.account_id ? 'account' : 'new',
      vpn: body.vpn ?? false, tariff_id: body.tariff_id ?? null, account_id: body.account_id ?? null, note: body.note ?? '',
      status: 'active', created_at: now(), created_by: store().accounts[0]?.id ?? 'a', expires_at: later(body.days ?? 7),
      used_at: null, used_account_id: null };
    if (!body.account_id) store().invites.unshift(invite);
    const created: Schemas['CreatedInvite'] = { ...invite, token, cabinet_link: `${location.origin}/i/${token}`,
      telegram_link: body.account_id ? null : `https://t.me/${store().bot.username}?start=${token}`,
      telegram_bot_username: body.account_id ? null : store().bot.username };
    return HttpResponse.json(created, { status: 201 });
  })),
  http.delete('/api/v1/invites/:id', guarded((_, { id }) => {
    const invite = store().invites.find((item) => item.id === id);
    if (!invite) return error(404, 'resource_not_found');
    invite.status = 'revoked';
    return invite;
  })),
  http.get('/api/v1/invites/telegram-bot', guarded(() => store().bot)),
  http.put('/api/v1/invites/telegram-bot', guarded(async (request) => {
    const { username } = (await request.json()) as Schemas['TelegramBotUsernameInput'];
    if (!/^[A-Za-z0-9_]{2,29}bot$/i.test(username)) return error(422, 'telegram_bot_username_invalid');
    store().bot.username = username;
    return store().bot;
  })),
  http.get('/api/v1/system/registration', guarded(() => store().registration)),
  http.patch('/api/v1/system/registration', guarded(async (request) => {
    const body = (await request.json()) as Schemas['RegistrationSettingsInput'];
    if (body.open != null) store().registration.open = body.open;
    if ('cleanup_days' in body) store().registration.cleanup_days = body.cleanup_days ?? null;
    return store().registration;
  })),

  http.get('/api/v1/merge-requests', guarded(() => ({ items: store().merges }))),
  http.get('/api/v1/merge-requests/:id', guarded((_, { id }) =>
    store().merges.find((item) => item.id === id) ?? error(404, 'resource_not_found'))),
  http.post('/api/v1/merge-requests/:id/decision', guarded(async (request, { id }) => {
    const merge = store().merges.find((item) => item.id === id);
    if (!merge) return error(404, 'resource_not_found');
    const body = (await request.json()) as Schemas['MergeDecisionInput'];
    if (body.decision === 'approve' && !body.keep_profile) return error(422, 'merge_profile_choice_required');
    Object.assign(merge, { status: body.decision === 'approve' ? 'approved' : 'rejected', decided_at: now(),
      keep_profile: body.keep_profile ?? null });
    return merge;
  }))
];

/** The accounts list from the same store, so a change shows there too. */
export function peopleAccounts(access: string): Account[] {
  return store().accounts.filter((item, index) => access === 'all'
    || (access === 'without') === (item.profile_id == null && index % 3 === 2));
}
