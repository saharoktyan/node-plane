/** The VPN policy keys staff set for the installation, a tariff, a profile
 * or a device: how each is entered and shown. The controller says which
 * keys a subject takes; a key it does not know here is left out. */

export type PolicyKey = {
  key: string; value: unknown; revision: number; default: unknown; effective: unknown; tariff?: unknown;
};

type Kind =
  | { type: 'choice'; values: string[] }
  | { type: 'number'; unit: 'count' | 'seconds' | 'days'; min: number; max: number }
  | { type: 'bytes' }
  | { type: 'speed'; min: number }
  | { type: 'text' };

export type Field = Kind & {
  key: string; group: 'devices' | 'connections' | 'quota' | 'speed' | 'xray';
  /** A line under the field while an own value is entered. */
  hint?: boolean;
  /** Shown only while another key has this value. */
  when?: [string, string[]];
};

export const FIELDS: Field[] = [
  { key: 'devices.max', group: 'devices', type: 'number', unit: 'count', min: 1, max: 1000 },
  { key: 'connections.mode', group: 'connections', hint: true, type: 'choice', values: ['off', 'observe', 'enforce'] },
  { key: 'connections.max_sources', group: 'connections', type: 'number', unit: 'count', min: 1, max: 100,
    when: ['connections.mode', ['observe', 'enforce']] },
  { key: 'connections.window_seconds', group: 'connections', type: 'number', unit: 'seconds', min: 30, max: 3600 },
  { key: 'connections.cooldown_seconds', group: 'connections', type: 'number', unit: 'seconds', min: 0, max: 86400 },
  { key: 'quota.bytes', group: 'quota', hint: true, type: 'bytes' },
  { key: 'quota.reset', group: 'quota', hint: true, type: 'choice', values: ['calendar_month', 'monthly_anchor', 'interval', 'none'] },
  { key: 'quota.interval_days', group: 'quota', type: 'number', unit: 'days', min: 1, max: 366,
    when: ['quota.reset', ['interval']] },
  { key: 'quota.on_exhausted', group: 'quota', type: 'choice', values: ['freeze', 'notify', 'throttle'] },
  { key: 'quota.throttle_kbit', group: 'quota', type: 'speed', min: 64, when: ['quota.on_exhausted', ['throttle']] },
  { key: 'quota.timezone', group: 'quota', type: 'text' },
  { key: 'speed.up_kbit', group: 'speed', hint: true, type: 'speed', min: 0 },
  { key: 'speed.down_kbit', group: 'speed', hint: true, type: 'speed', min: 0 },
  { key: 'xray.bittorrent', group: 'xray', type: 'choice', values: ['allow', 'block'] }
];

/** Keys the installation may set for one node. */
export const NODE_KEYS = ['connections.mode', 'connections.max_sources', 'speed.up_kbit', 'speed.down_kbit', 'xray.bittorrent'];

/** A stored value of a subject, global or for one node. */
export type PolicyValue = { key: string; node_key: string | null; protocol: string | null; value: unknown; revision: number };

export const GROUPS = ['devices', 'connections', 'quota', 'speed', 'xray'] as const;
const GIB = 2 ** 30;

/** What a value is entered as: GiB for bytes and Mbit/s for a speed. */
export function toInput(field: Field, value: unknown): string {
  if (value == null) return '';
  if (field.type === 'bytes') return String(Math.round(((value as number) / GIB) * 100) / 100);
  if (field.type === 'speed') return String((value as number) / 1000);
  return String(value);
}

/** The stored value of what was entered, or null when it is out of range. */
export function fromInput(field: Field, input: string): unknown {
  if (field.type === 'choice') return field.values.includes(input) ? input : null;
  if (field.type === 'text') return input.trim() || null;
  const number = Number(input.replace(',', '.'));
  if (input.trim() === '' || !Number.isFinite(number) || number < 0) return null;
  if (field.type === 'bytes') return Math.round(number * GIB);
  if (field.type === 'speed') {
    const kbit = Math.round(number * 1000);
    return kbit === 0 && field.min === 0 ? 0 : kbit >= Math.max(field.min, 1) && kbit <= 100_000_000 ? kbit : null;
  }
  return Number.isInteger(number) && number >= field.min && number <= field.max ? number : null;
}

/** Whether a field is in play with these values. */
export function shown(field: Field, values: Record<string, unknown>) {
  return !field.when || field.when[1].includes(String(values[field.when[0]]));
}
