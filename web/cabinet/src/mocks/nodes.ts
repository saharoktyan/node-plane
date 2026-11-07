/** Servers on mocks: the fixtures' nodes and an invitation waiting for its
 * agent, kept for the tab so a saved setting, an applied change or an
 * install command shows on the next read; the node finishes in a moment. */
import { http, HttpResponse } from 'msw';
import type { Schemas } from '$lib/api/client';
import * as fixtures from './fixtures';
import { error, guarded } from './people';
import { choice } from './scenario';

type Node = Schemas['AdminNodeOutput'] & Record<string, unknown>;
type Extra = { agent: 'ready' | 'missing' | 'unavailable'; enrollment: string | null; operation: Record<string, unknown> | null };
let state: { scenario: string; nodes: Node[]; extra: Record<string, Extra> } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const nodes = fixtures.nodes(scenario).map((node, index) => ({ ...node, stage: 'node', transport: 'remote',
    ssh_target: `root@${node.key}.example.net`, xray_transports: node.protocols.includes('xray') ? ['tcp', 'xhttp'] : [],
    agent: { method: index ? 'token' : 'local', version: '0.5.0', installed_at: '2026-10-01T10:00:00+00:00', checked_at: null },
    settings: { public_host: `${node.key}.example.net`, awg_port: 443, awg_port_mode: 'auto', awg_i1_preset: 'quic',
      xray_sni: 'www.cloudflare.com', xray_tcp_port: 443, xray_xhttp_port: 8443, xray_xhttp_path: '/assets' } })) as Node[];
  if (scenario !== 'empty') nodes.push({ key: 'pl1', title: 'Warsaw', flag: '🇵🇱', region: 'Europe', region_id: 'europe',
    enabled: true, protocols: ['awg', 'xray'], xray_transports: ['tcp'], desired_revision: 1, applied_revision: 0, notes: '',
    policy_eligible: false, settings_apply_pending: false, setup_attempted: false, stage: 'invitation', transport: 'remote',
    ssh_target: null, agent: null, service_instances: [], settings: { public_host: 'pl1.example.net' } } as unknown as Node);
  const extra: Record<string, Extra> = {};
  for (const node of nodes) extra[node.key] = { agent: node.stage === 'invitation' ? 'missing' : 'ready', enrollment: null, operation: null };
  state = { scenario, nodes, extra };
  return state;
}

const find = (key: string) => store().nodes.find((node) => node.key === key);

/** Something the node does: running for a moment, then done. */
function work(key: string, kind: string, finish: () => void) {
  const operation = { id: crypto.randomUUID(), node_key: key, status: 'running', kind,
    progress: { stage: kind === 'bootstrap' ? 'protocol_deploy' : 'restart_driver', label: '' } };
  store().extra[key].operation = operation;
  setTimeout(() => {
    operation.status = 'succeeded';
    finish();
  }, 5000);
  return operation;
}

const TEMPLATES = [['de', 'Frankfurt', 'Europe', '🇩🇪'], ['nl', 'Amsterdam', 'Europe', '🇳🇱'], ['fi', 'Helsinki', 'Europe', '🇫🇮'],
  ['us', 'New York', 'North America', '🇺🇸'], ['sg', 'Singapore', 'Asia', '🇸🇬']];

/** The next free number after a location's code, as the controller picks it. */
function nextKey(prefix: string) {
  for (let number = 1; ; number += 1) if (!find(prefix + number)) return prefix + number;
}

export const nodeHandlers = [
  http.get('/api/v1/nodes/creation-options', guarded(() => ({ local_available: true,
    defaults: { protocols: ['awg', 'xray'], xray_transports: ['tcp', 'xhttp'],
      settings: { awg_port: 443, awg_port_mode: 'auto', awg_i1_preset: 'quic', xray_sni: 'www.cloudflare.com' } },
    ssh_preparation_required: false,
    templates: TEMPLATES.map(([code, title, region, flag]) => ({ code, title, region, flag,
      draft: { key: nextKey(code), title } })) }))),
  http.get('/api/v1/nodes/key-availability', guarded((request) => {
    const key = new URL(request.url).searchParams.get('key') ?? '';
    const prefix = key.replace(/\d+$/, '');
    return find(key) ? { key, available: false, reason: 'node_key_conflict', suggestion: nextKey(prefix) }
      : key === 'ru1' ? { key, available: false, reason: 'node_key_retired', suggestion: nextKey('ru') }
      : { key, available: true, reason: null, suggestion: null };
  })),
  http.post('/api/v1/nodes', guarded(async (request) => {
    const body = (await request.json()) as Record<string, unknown>;
    const template = TEMPLATES.find(([code]) => code === body.template);
    const key = template ? nextKey(template[0]) : String(body.key);
    if (find(key)) return error(409, 'node_key_conflict');
    const node = { key, title: body.title, flag: body.flag, region: body.region, region_id: String(body.region).toLowerCase(),
      enabled: true, protocols: body.protocols, xray_transports: body.xray_transports, desired_revision: 1, applied_revision: 0,
      notes: '', policy_eligible: false, settings_apply_pending: false, setup_attempted: false, stage: 'invitation',
      transport: body.transport, ssh_target: body.ssh_target ?? null, agent: null, service_instances: [],
      settings: body.settings } as unknown as Node;
    store().nodes.push(node);
    store().extra[key] = { agent: 'missing', enrollment: null, operation: null };
    return HttpResponse.json(node, { status: 201 });
  })),
  http.get('/api/v1/nodes', guarded((request) => {
    const stage = new URL(request.url).searchParams.get('stage');
    return { items: store().nodes.filter((node) => !stage || node.stage === stage), next_cursor: null };
  })),
  http.get('/api/v1/nodes/:key', guarded((_, { key }) => find(key) ?? error(404, 'resource_not_found'))),
  http.get('/api/v1/nodes/:key/overview', guarded((_, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    const operation = store().extra[key].operation as { kind: string } | null;
    const applying = operation && (operation as { status?: string }).status === 'running';
    const state = node.applied_revision === 0 ? 'not_installed' : !node.enabled ? 'inactive'
      : applying ? 'applying' : node.applied_revision < node.desired_revision ? 'changes_pending' : 'applied_unverified';
    return { node_key: key, state, enabled: node.enabled, applied_revision: node.applied_revision,
      desired_revision: node.desired_revision, settings_complete: true, settings_task_status: null,
      access_total: node.stage === 'node' ? 6 : 0, ready: 5, pending: 1, failed: 0, attention: 0,
      bootstrap: operation?.kind === 'bootstrap' ? operation : null,
      settings_operation: operation?.kind === 'settings' ? operation : null, last_job: null, agent_rollout: null,
      traffic: node.stage === 'node' ? { status: 'current', month: '2026-11', total_bytes: 412 * 2 ** 30, items: [
        { protocol: 'xray', status: 'current', uplink_bytes: 40 * 2 ** 30, downlink_bytes: 260 * 2 ** 30,
          inbounds: { 'vless-tcp': { uplink_bytes: 30 * 2 ** 30, downlink_bytes: 190 * 2 ** 30 },
            'vless-xhttp': { uplink_bytes: 10 * 2 ** 30, downlink_bytes: 70 * 2 ** 30 } } },
        { protocol: 'awg', status: 'current', uplink_bytes: 22 * 2 ** 30, downlink_bytes: 90 * 2 ** 30 }] } : null };
  })),
  http.get('/api/v1/nodes/:key/services', guarded((_, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    const agent = store().extra[key].agent;
    if (agent === 'missing') return error(409, 'node_agent_unconfigured');
    if (agent === 'unavailable') return error(503, 'node_agent_unavailable');
    const ready = node.applied_revision > 0;
    return { docker: true, awg_running: ready && node.protocols.includes('awg'), xray_running: ready && node.protocols.includes('xray'),
      awg_installed: ready, xray_installed: ready, awg_parameters: {} };
  })),
  http.get('/api/v1/nodes/:key/agent-enrollment', guarded((_, { key }) => {
    const status = store().extra[key]?.enrollment;
    return status ? { node_key: key, status, expires_at: new Date(Date.now() + 3_600_000).toISOString(), error_code: null,
      ssh_install: null, target: null, restrict_to_controller: false } : { status: 'none' };
  })),
  http.post('/api/v1/nodes/:key/agent-enrollment', guarded(async (request, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    const body = (await request.json()) as Schemas['AgentEnrollmentInput'];
    const extra = store().extra[key];
    extra.enrollment = 'waiting';
    // The command runs on the server a little later; the agent then installs the protocols.
    setTimeout(() => {
      extra.enrollment = 'completed';
      extra.agent = 'ready';
      node.stage = 'node';
      work(key, 'bootstrap', () => (node.applied_revision = node.desired_revision));
    }, body.over_ssh ? 4000 : 8000);
    const expires = new Date(Date.now() + 3_600_000).toISOString();
    return HttpResponse.json({ command: `curl -fsSL https://controller.example.net/agent/install.sh | sh -s -- --node ${key} --token npa_${crypto.randomUUID().slice(0, 18)}`,
      expires_at: expires, target: null, restrict_to_controller: body.restrict_to_controller, bootstrap: null }, { status: 201 });
  })),
  http.post('/api/v1/nodes/:key/bootstrap', guarded((_, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    return HttpResponse.json(work(key, 'bootstrap', () => (node.applied_revision = node.desired_revision)), { status: 202 });
  })),
  http.post('/api/v1/nodes/:key/agent-rollouts', guarded((_, { key }) => {
    const extra = store().extra[key];
    setTimeout(() => (extra.agent = 'ready'), 3000);
    return HttpResponse.json({ id: crypto.randomUUID(), status: 'running' }, { status: 202 });
  })),
  http.patch('/api/v1/nodes/:key', guarded(async (request, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    if (request.headers.get('If-Match') !== `"${node.desired_revision}"`) return error(412, 'revision_conflict');
    const body = (await request.json()) as Record<string, unknown>;
    const { confirm_access_change: _, settings, ...rest } = body;
    Object.assign(node, rest);
    if (settings) node.settings = settings as Node['settings'];
    if (rest.region) node.region_id = String(rest.region).toLowerCase();
    if (settings || rest.protocols || rest.xray_transports) {
      node.desired_revision += 1;
      node.settings_apply_pending = node.applied_revision > 0;
    }
    return node;
  })),
  http.get('/api/v1/nodes/:key/region-access-preview', guarded((request, { key }) => {
    const region = new URL(request.url).searchParams.get('region') ?? '';
    const changed = find(key)?.region !== region;
    return { node_key: key, region, region_id: region.toLowerCase(), affected_profiles: changed ? 2 : 0, profiles: [] };
  })),
  http.post('/api/v1/nodes/:key/apply-settings', guarded((_, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    node.settings_apply_pending = false;
    const operation = work(key, 'settings', () => (node.applied_revision = node.desired_revision));
    return HttpResponse.json({ ...operation, revision: node.desired_revision, services: node.protocols }, { status: 202 });
  })),
  http.get('/api/v1/system/installation-defaults', guarded(() => ({ revision: 1, settings: { xray_sni: 'www.cloudflare.com' } })))
];
