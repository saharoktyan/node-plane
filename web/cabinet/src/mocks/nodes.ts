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

const jobs = new Map<string, { status: string; result: unknown }>();
const removals = new Map<string, Record<string, unknown>>();
const recovered = new Set<string | null>();

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
      awg_installed: ready, xray_installed: ready, awg_config_valid: ready, xray_config_valid: ready, runtime_drift: false, awg_parameters: {} };
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
  http.get('/api/v1/nodes/:key/diagnostics', guarded((_, { key }) => {
    const node = find(key);
    if (!node) return error(404, 'resource_not_found');
    if (store().extra[key].agent !== 'ready') return error(409, 'node_agent_unavailable');
    const flag = (id: string, status = 'ok', detail = 'Confirmed') => ({ id, status, detail });
    return { node_key: key, docker: 'ok', runtime_root: 'ok', awg_config: 'ok', xray_config: 'ok', runtime_version: '0.5.0',
      checked_at: new Date().toISOString(), checks: [flag('agent', 'ok', 'Agent responded to the live inspection'),
        flag('agent_version', 'ok', 'Agent version: 0.5.0; expected: 0.5.0'), flag('docker'),
        ...node.protocols.flatMap((p) => ['installed', 'running', 'config_valid'].map((f) => flag(`${p}:${f}`))),
        flag('settings', node.applied_revision < node.desired_revision ? 'warning' : 'ok',
          node.applied_revision < node.desired_revision ? `Settings awaiting application: ${node.applied_revision} → ${node.desired_revision}`
            : 'Desired and applied settings revisions match'),
        flag('runtime_version'), flag('cpu_used_percent', 'ok', 'CPU used (1-minute average): 7%; warning ≥80%, error ≥95%'),
        flag('ram_used_percent', 'warning', 'RAM used: 86%; warning ≥85%, error ≥95%'), flag('disk_free_percent', 'ok', 'Disk free: 64%'),
        flag('load1', 'ok', 'Load average per CPU: 0.2')],
      observation: { response_ms: 84, host_metrics: { cpu_used_percent: 7, ram_used_percent: 86, ram_total_bytes: 2 * 2 ** 30,
        ram_available_bytes: 0.28 * 2 ** 30, disk_free_percent: 64, disk_total_bytes: 40 * 2 ** 30, disk_free_bytes: 25.6 * 2 ** 30,
        load1: 0.4, load5: 0.3, load15: 0.2, cpus: 2 },
        protocols: node.protocols.map((protocol) => ({ protocol, ports: protocol === 'awg' ? [{ network: 'udp', port: 443 }]
          : [{ transport: 'tcp', network: 'tcp', port: 443 }, { transport: 'xhttp', network: 'tcp', port: 8443 }] })) } };
  })),
  http.get('/api/v1/system/recovery', guarded((request) => {
    const key = new URL(request.url).searchParams.get('node_key');
    const items = (key === 'de1-0' ? [{ id: 'c0ffee00-0000-4000-8000-000000000001', kind: 'settings', status: 'blocked',
      error_code: 'outcome_unconfirmed', next_step: 'journal', phase: 'applying', actions: [...recovered.has(key) ? [] : ['recheck', 'resolve', 'force']],
      node_key: key, related_id: '', related_kind: '', subject_id: key }] : []).filter((item) => item.actions.length);
    return { grouped: false, items, total: items.length, offset: 0, maintenance_active: false, node_key: key, node_title: '' };
  })),
  http.post('/api/v1/system/recovery/:kind/:id/:action', guarded(() => {
    recovered.add('de1-0');
    return { outcome: 'confirmed' };
  })),
  http.post('/api/v1/nodes/:key/actions', guarded(async (request, { key }) => {
    const body = (await request.json()) as { action: string };
    const job = { id: crypto.randomUUID(), node_key: key, action: body.action, status: 'running', result: null as unknown };
    jobs.set(job.id, job);
    setTimeout(() => {
      job.status = 'succeeded';
      if (body.action === 'check_ports') job.result = { ports: [{ port: 443, protocol: 'udp', status: 'listening' },
        { port: 443, protocol: 'tcp', status: 'listening' }, { port: 8443, protocol: 'tcp', status: 'free' }], firewall: 'unmanaged' };
    }, 3000);
    return HttpResponse.json(job, { status: 202 });
  })),
  http.get('/api/v1/node-jobs/:id', guarded((_, { id }) => jobs.get(id) ?? error(404, 'resource_not_found'))),
  http.get('/api/v1/nodes/:key/removal', guarded((_, { key }) => removals.get(key) ?? error(404, 'resource_not_found'))),
  http.get('/api/v1/nodes/:key/maintenance', guarded((_, { key }) => ({ node_key: key, status: 'active', affected_profiles: 6,
    pending_tasks: 0, blocked_tasks: 0, cleanup_phase: null, revocations_complete: false, operation_ids: [], verification_target: null }))),
  http.post('/api/v1/nodes/:key/remove-step', guarded((_, { key }) => {
    const removal = { node_key: key, status: 'draining', removal_status: 'running', cleanup_phase: 'not_started',
      revocations_complete: false, pending_tasks: 6, blocked_tasks: 0, affected_profiles: 6, error_code: null } as Record<string, unknown>;
    removals.set(key, removal);
    const steps: [number, Record<string, unknown>][] = [[3000, { revocations_complete: true, pending_tasks: 0, cleanup_phase: 'prepared' }],
      [6000, { cleanup_phase: 'runtime_deleted' }], [9000, { cleanup_phase: 'uninstall_scheduled' }]];
    for (const [delay, change] of steps) setTimeout(() => Object.assign(removal, change), delay);
    setTimeout(() => {
      removals.set(key, { node_key: key, status: 'removed', checked_by: 'agent' });
      store().nodes = store().nodes.filter((node) => node.key !== key);
    }, 12000);
    return removal;
  })),
  http.post('/api/v1/nodes/:key/retire-registry-only', guarded((_, { key }) => {
    store().nodes = store().nodes.filter((node) => node.key !== key);
    removals.set(key, { node_key: key, status: 'removed_registry_only' });
    return { node_key: key, mode: 'registry_only' };
  }))
];
