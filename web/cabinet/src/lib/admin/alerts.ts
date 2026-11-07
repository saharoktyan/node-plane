/** Server alerts as the bot and Workstation word them: what is wrong and,
 * for a resource, the reading that raised it. */

export type Alert = { node_key: string; kind: string; title: string; flag?: string | null; first_seen_at: string;
  last_seen_at: string; event_id: string | null; dismissed: boolean; payload: { value?: number; cpus?: number } };

export const KINDS = ['node_unreachable', 'awg_down', 'xray_down', 'cpu_high', 'ram_high', 'load_high', 'disk_low'];
/** Repeats of an alert still open, in minutes; none repeats it never. */
export const REPEATS = [0, 30, 60, 180] as const;

/** The alert's name to translate, and its reading when it has one. */
export function describe(alert: Alert): { key: string; reading: { key: string; value: string; cpus?: number } | null } {
  const key = 'kind_' + (KINDS.includes(alert.kind) ? alert.kind : 'other');
  const value = alert.payload?.value;
  if (typeof value !== 'number' || !Number.isFinite(value) || !['disk_low', 'ram_high', 'cpu_high', 'load_high'].includes(alert.kind))
    return { key, reading: null };
  return { key, reading: { key: 'reading_' + alert.kind, value: value.toFixed(alert.kind === 'load_high' ? 2 : 1),
    ...(alert.kind === 'load_high' ? { cpus: alert.payload.cpus } : {}) } };
}

/** Open alerts first, the ones already seen after, each by server. */
export const ordered = (items: Alert[]) =>
  [...items].sort((a, b) => Number(a.dismissed) - Number(b.dismissed) || a.title.localeCompare(b.title));
