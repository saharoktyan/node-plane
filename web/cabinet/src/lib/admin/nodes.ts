/** A server as the node pages read it, with the state, agent and protocol
 * lines the bot and Workstation show for it. */
import type { Schemas } from '$lib/api/client';

export type Node = Schemas['AdminNodeOutput'] & {
  transport?: string | null; ssh_target?: string | null; xray_transports?: string[];
  stage?: string; settings?: Record<string, unknown>;
};
export type Overview = Schemas['AdminNodeOverviewOutput'];
/** What the agent reports of the services, or why it could not. */
export type Agent = 'ready' | 'missing' | 'unavailable' | 'identity_mismatch' | 'inspection_failed' | 'unknown';
export type Services = Record<string, unknown>;

/** The agent's state from the error its services read gave. */
export function agentOf(code: string | null): Agent {
  if (code === null) return 'ready';
  const known: Record<string, Agent> = {
    node_agent_unconfigured: 'missing', node_agent_unavailable: 'unavailable',
    node_agent_identity_mismatch: 'identity_mismatch', node_agent_inspection_failed: 'inspection_failed'
  };
  return known[code] ?? 'unknown';
}

export const installed = (node: Node) => (node.stage ? node.stage === 'node' : node.applied_revision > 0);

export function state(node: Node, overview?: Overview | null): string {
  if (overview?.state) return overview.state;
  if (node.applied_revision === 0) return 'not_installed';
  if (!node.enabled) return 'inactive';
  return node.applied_revision < node.desired_revision ? 'changes_pending' : 'applied_unverified';
}

/** The state to show: before its agent a server is only an invitation. */
export function stateKey(node: Node, overview?: Overview | null) {
  const value = state(node, overview);
  return !installed(node) && !['deleting', 'deletion_blocked'].includes(value) ? 'invitation' : value;
}

export function pendingSettings(node: Node) {
  return node.settings_apply_pending
    ?? (node.applied_revision > 0 && node.protocols.length > 0 && node.applied_revision < node.desired_revision);
}

export const canBootstrap = (node: Node, overview: Overview | null) =>
  state(node, overview) === 'not_installed' && overview?.settings_complete === true;

export function needsSetup(node: Node, overview: Overview | null, agent: Agent, services: Services | null) {
  return overview?.settings_complete === true
    && (canBootstrap(node, overview) || agent === 'missing' || services?.docker === false);
}

export function protocolStatus(node: Node, services: Services | null, protocol: 'awg' | 'xray') {
  if (!services) return 'unchecked';
  if (services[protocol + '_running'] === true) return 'running';
  if (services.docker === false) return 'unavailable';
  const present = services[protocol + '_installed'];
  if (present === true) return 'stopped';
  if (present === false) return 'missing';
  if (services[protocol + '_config_valid'] === true) return 'stopped';
  if (node.applied_revision === 0 || !node.protocols.includes(protocol)) return 'missing';
  return 'unchecked';
}

export const hasService = (node: Node, service: string) =>
  ((node.service_instances ?? []) as { service: string }[]).some((item) => item.service === service);

/** The protocols' names as people know them. */
export const protocolName = (protocol: string) => (protocol === 'awg' ? 'AmneziaWG' : 'VLESS');
