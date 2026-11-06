/** Mock data. Every value is checked against the API's types, so a change
 * of the contract that the cabinet does not follow fails `npm run check`. */
import type { Schemas } from '$lib/api/client';
import type { Role, Scenario } from './scenario';

const ADMIN_PERMISSIONS = ['access_requests.manage', 'accounts.manage', 'profiles.manage', 'grants.manage',
  'configs.manage.issue', 'configs.manage.read', 'nodes.manage', 'nodes.inspect', 'nodes.execute',
  'maintenance.manage', 'settings.manage', 'diagnostics.sensitive.read', 'services.xray.manage',
  'services.awg.manage', 'services.relay.manage'];
const MEMBER_PERMISSIONS = ['account.self.read', 'account.self.preferences.write', 'access_requests.self.create',
  'access_requests.self.read', 'profiles.self.read', 'nodes.available.read', 'configs.self.issue',
  'configs.self.read', 'operations.read'];
const ROLE_PERMISSIONS: Record<Role, string[]> = {
  owner: [...ADMIN_PERMISSIONS, 'roles.manage'],
  admin: ADMIN_PERMISSIONS,
  operator: ['nodes.manage', 'nodes.inspect', 'nodes.execute', 'maintenance.manage', 'diagnostics.sensitive.read',
    'services.xray.manage', 'services.awg.manage'],
  support: ['access_requests.manage', 'profiles.manage', 'configs.manage.read', 'configs.manage.issue'],
  member: []
};

const VPN_ID = '6f1c1a52-8f3e-4c8e-9b8a-0f6f2d0c1a11';
const OWN_ID = '9a7c2d1e-3b4f-4a5b-8c6d-7e8f9a0b1c22';

export function me(role: Role, scenario: Scenario, locale: 'ru' | 'en'): Schemas['MeOutput'] {
  return {
    id: '2b0c6d3a-1111-4222-8333-944455556666', role: role === 'member' ? 'member' : 'admin',
    roles: role === 'member' ? [] : [role], status: 'approved',
    permissions: [...MEMBER_PERMISSIONS, ...ROLE_PERMISSIONS[role]],
    login: 'nina', display_name: 'Нина', username: 'nina_tg', locale, locale_selected: true,
    services: scenario === 'empty' ? [] : ['vpn'], has_password: true, passkeys: 1,
    account_created: true, announcement_silent: false, onboarding_completed: true, service_notifications: 'on',
    traffic_available: true
  };
}

// Tariff titles are the administrator's text, in whatever language they wrote;
// the mocks follow the chosen language so screenshots read in one.
const MONTH = { ru: 'Месяц', en: 'Month' };

export function myServices(scenario: Scenario, lang: 'ru' | 'en'): Schemas['ServiceProfileOutput'][] {
  if (scenario === 'empty') return [];
  const vpn: Schemas['ServiceProfileOutput'] = {
    id: VPN_ID, service: 'vpn', kind: 'builtin', status: 'active', actions: ['devices', 'configs', 'renewal'],
    summary: { title: 'Нина', devices: 2, tariff: MONTH[lang], expires_at: '2026-12-06T00:00:00+00:00', renewal: null }
  };
  // A service from a manifest, drawn by the generic module.
  const own: Schemas['ServiceProfileOutput'] = {
    id: OWN_ID, service: 'vaultwarden', kind: 'user', status: 'active', actions: ['open'],
    summary: { address: 'https://vault.example.org', account: 'nina@example.org' }
  };
  return scenario === 'many' ? [vpn, own] : [vpn];
}

export function catalog(role: Role, scenario: Scenario): Schemas['ServiceOutput'][] {
  const staff = role !== 'member';
  const items: Schemas['ServiceOutput'][] = [{
    service: 'vpn', kind: 'builtin', profiles: true, instance_types: ['xray', 'awg'],
    access: scenario === 'empty' && !staff ? 'requestable' : 'active', state: null,
    instances: staff && role !== 'support' ? (scenario === 'many' ? 24 : 3) : null
  }];
  if (role === 'owner' || role === 'admin')
    items.push(
      { service: 'relay', kind: 'system', profiles: false, instance_types: ['relay'], access: null, state: 'active',
        instances: 1 },
      { service: 'telegram_bot', kind: 'system', profiles: false, instance_types: [], access: null, state: 'active',
        instances: null }
    );
  if (scenario === 'many')
    items.push({ service: 'vaultwarden', kind: 'user', profiles: true, instance_types: ['vaultwarden'],
      access: 'active', state: null, instances: staff ? 1 : null });
  return items;
}

export function summary(id: string, lang: 'ru' | 'en'): Schemas['MemberProfileSummary'] {
  return {
    profile_id: id, display_name: 'Нина', frozen: false, expired: false, quota_suspended: false,
    tariff: { id: 't1', title: MONTH[lang], duration_days: 30, assigned_at: '2026-11-06T00:00:00+00:00' },
    renewal: null, quota: { limit_bytes: 107374182400, used_bytes: 32212254720, period_end: '2026-12-01T00:00:00+00:00',
      on_exhausted: 'block', throttled_kbit: null },
    expires_at: '2026-12-06T00:00:00+00:00', created_at: '2026-10-01T00:00:00+00:00',
    nodes: [
      { key: 'de1', title: 'Frankfurt', flag: '🇩🇪', region: 'Europe', protocols: ['xray', 'awg'] },
      { key: 'fi1', title: 'Helsinki', flag: '🇫🇮', region: 'Europe', protocols: ['xray'] }
    ],
    node_count: 2, protocol_count: 3, xray_count: 2, awg_count: 1, issued_count: 4,
    last_issued_at: '2026-11-01T10:00:00+00:00'
  };
}

export const sessions: Schemas['SessionOutput'][] = [
  { id: 's1', method: 'password', device: 'Firefox on Linux', created_at: '2026-11-06T09:00:00+00:00',
    used_at: '2026-11-06T12:00:00+00:00', expires_at: '2026-12-06T12:00:00+00:00', current: true },
  { id: 's2', method: 'telegram', device: 'Telegram Mini App', created_at: '2026-11-01T09:00:00+00:00',
    used_at: '2026-11-05T20:00:00+00:00', expires_at: '2026-12-05T20:00:00+00:00', current: false }
];

export const passkeys: Schemas['PasskeyOutput'][] = [
  { id: 'p1', name: 'Phone', created_at: '2026-10-20T09:00:00+00:00', used_at: '2026-11-05T09:00:00+00:00',
    backed_up: true }
];

export function overview(scenario: Scenario): Schemas['AdminOverviewOutput'] {
  const many = scenario === 'many';
  return {
    version: '0.5.0-dev', nodes_total: many ? 24 : 3, nodes_enabled: many ? 22 : 3,
    profiles_total: many ? 480 : 12, profiles_active: many ? 455 : 11, profiles_frozen: many ? 25 : 1,
    pending_requests: many ? 7 : 1, requests_enabled: true, invitations_total: 0, alerts_enabled: true, unacknowledged_alerts: many ? 3 : 0,
    problem_nodes: many ? [{ key: 'sg1', title: 'Singapore', region: 'Asia', flag: '🇸🇬' }] : [],
    checked_at: '2026-11-06T12:00:00+00:00'
  };
}

export function accounts(scenario: Scenario, access: string): Schemas['AdminAccountOutput'][] {
  const count = scenario === 'empty' ? 0 : scenario === 'many' ? 60 : 4;
  return Array.from({ length: count }, (_, index) => ({
    id: `a${index}-0000-4000-8000-000000000000`, role: index === 0 ? 'admin' : 'member',
    roles: index === 0 ? ['owner'] : [], status: 'approved', revision: 1,
    telegram_user_id: index % 2 ? 100 + index : null, username: index % 2 ? `user${index}` : null,
    login: index % 2 ? null : `user${index}`, display_name: `User ${index}`, account_created: true, deleted: false,
    has_password: index % 2 === 0, locale_selected: false, passkeys: 0, sessions: 0
  })).filter((_, index) => access === 'all' || (access === 'without') === (index % 3 === 2));
}

export function nodes(scenario: Scenario): Schemas['AdminNodeOutput'][] {
  const titles = [['de1', 'Frankfurt', '🇩🇪'], ['fi1', 'Helsinki', '🇫🇮'], ['nl1', 'Amsterdam', '🇳🇱']];
  const count = scenario === 'empty' ? 0 : scenario === 'many' ? 24 : 3;
  return Array.from({ length: count }, (_, index) => {
    const [key, title, flag] = titles[index % 3];
    return {
      key: `${key}-${index}`, title: `${title} ${index + 1}`, flag, region: 'Europe', enabled: index !== 5,
      protocols: ['xray', 'awg'], xray_transports: ['tcp'], desired_revision: 3, applied_revision: index === 4 ? 2 : 3,
      settings: {}, notes: '', policy_eligible: true, settings_apply_pending: index === 4, setup_attempted: true,
      stage: 'node', service_instances: [
        { id: `x${index}`, service: 'xray', state: index === 4 ? 'changes_pending' : 'active' },
        { id: `w${index}`, service: 'awg', state: 'active' },
        ...(index === 0 ? [{ id: 'r0', service: 'relay', state: 'active' }] : [])
      ]
    };
  });
}

export const inbox = [
  { id: 'n1', announcement_id: 'a1', text: 'Плановые работы на ноде Frankfurt в субботу с 03:00 до 04:00 UTC.',
    created_at: '2026-11-05T18:00:00+00:00', viewed_at: null },
  { id: 'n2', announcement_id: 'a2', text: 'Доступ к VPN продлён до 6 декабря.',
    created_at: '2026-11-01T10:00:00+00:00', viewed_at: '2026-11-01T11:00:00+00:00' }
];
