/** A server's maintenance as the bot offers it: which actions fit its
 * state, how a diagnostic check is named, and which recovery action takes
 * which path, as Workstation sends it. */
import type { Node, Services } from '$lib/admin/nodes';

export type Action = 'install_docker' | 'reinstall_keep' | 'reinstall_clean' | 'check_ports' | 'open_ports'
  | 'sync_env' | 'sync_xray' | 'sync_runtime' | 'reconcile_access' | 'regenerate_entropy' | 'cleanup_runtime';

/** Ones that cost users their configurations or connections. */
export const DANGER: Action[] = ['reinstall_clean', 'regenerate_entropy', 'cleanup_runtime'];

/** The groups of actions this server can take now; none before its VPN. */
export function actions(node: Node, services: Services | null): [string, Action[]][] {
  if (!services || !node.protocols.length || node.applied_revision === 0) return [];
  const reusable = node.protocols.every((protocol) => services[protocol + '_config_valid'] === true);
  const install: Action[] = [...(services.docker === false ? ['install_docker' as const] : []),
    ...(reusable ? ['reinstall_keep' as const] : []), 'reinstall_clean'];
  const repair: Action[] = ['sync_env', ...(node.protocols.includes('xray') ? ['sync_xray' as const] : []),
    ...(services.runtime_drift === true ? ['sync_runtime' as const] : []), 'reconcile_access',
    ...(node.protocols.includes('awg') ? ['regenerate_entropy' as const] : [])];
  return [['install', install], ['ports', ['check_ports', 'open_ports']], ['repair', repair], ['cleanup', ['cleanup_runtime']]];
}

const NAMES: Record<string, string> = {
  agent: 'agent', agent_version: 'agent_version', docker: 'docker', settings: 'settings', runtime_version: 'runtime',
  ram_used_percent: 'ram', disk_free_percent: 'disk', cpu_used_percent: 'cpu', load1: 'load', disabled: 'disabled',
  registry: 'registry', operations: 'operations', live_services: 'live'
};

/** A check's locale key with its protocol, or its own id when it is new. */
export function checkName(id: string): { key: string; protocol?: string } | { raw: string } {
  const [protocol, field] = id.split(':');
  if (field && (protocol === 'awg' || protocol === 'xray')) return { key: 'check_' + field, protocol };
  return id in NAMES ? { key: 'check_' + NAMES[id] } : { raw: id.replaceAll('_', ' ') };
}

const order: Record<string, number> = { error: 0, warning: 1, ok: 2 };
export const byStatus = <T extends { status: string }>(checks: T[]) =>
  [...checks].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));

/** Where a recovery action goes, as Workstation's `recovery_path`. */
export function recoveryPath(kind: string, id: string, action: string): string | null {
  if (kind === 'removal' && action === 'resume') return `/api/v1/system/recovery/removal/${encodeURIComponent(id)}/resume`;
  if (['settings', 'profile', 'node', 'bootstrap'].includes(kind) && action === 'force') return `/api/v1/system/recovery/${kind}/${id}/force`;
  if (kind === 'update' && ['cancel', 'recheck', 'resume'].includes(action)) return `/api/v1/system/updates/jobs/${id}/${action}`;
  if (kind === 'backup' && ['resume', 'cancel'].includes(action)) return `/api/v1/system/recovery/backup/${id}/${action}`;
  if (kind === 'cleanup' && ['retry', 'abort'].includes(action)) return `/api/v1/system/cleanup/jobs/${id}/${action}`;
  if (kind === 'issuance' && action === 'cancel') return `/api/v1/system/recovery/issuance/${id}/cancel`;
  if ((['node', 'settings', 'profile', 'bootstrap', 'temporary'].includes(kind) && ['resolve', 'recheck'].includes(action))
    || (kind === 'agent' && ['recheck', 'resume'].includes(action)) || (kind === 'bootstrap' && action === 'cancel'))
    return `/api/v1/system/recovery/${kind}/${id}/${action}`;
  return null;
}

/** The explanation's locale key for a recovery action, most specific first. */
export function explanation(kind: string, action: string, has: (key: string) => boolean) {
  const own = `recovery_explain_${kind}_${action}`;
  return has(own) ? own : `recovery_explain_${action}`;
}
