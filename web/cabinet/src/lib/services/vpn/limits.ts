/** What the tariff includes, in words: the profile's limits on every node
 * (`MemberProfileSummary.limits`), each marked when the profile sets it
 * itself rather than taking the tariff's or the installation's. */
import type { Schemas } from '$lib/api/client';
import { t } from '$lib/i18n/index.svelte';
import { bytes } from '$lib/ui/format';

export interface Line {
  label: string;
  value: string;
  own: boolean;
}

export function speed(kbit: number): string {
  return kbit >= 1000 ? t('vpn.mbit', { value: +(kbit / 1000).toFixed(1) }) : t('vpn.kbit', { value: kbit });
}

/** The lines to show, in a fixed order; a limit not in force is said so,
 * connection limits only when they apply. */
export function lines(limits: Schemas['MemberLimit'][]): Line[] {
  const of = new Map(limits.map((item) => [item.key, item]));
  const value = (key: string) => of.get(key)?.value ?? null;
  const own = (...keys: string[]) => keys.some((key) => of.get(key)?.own);
  const result: Line[] = [];

  const devices = value('devices.max');
  result.push({ label: t('vpn.limit_devices'), value: typeof devices === 'number' ? String(devices) : t('vpn.no_limit'),
    own: own('devices.max') });

  const quota = value('quota.bytes');
  if (typeof quota === 'number' && quota > 0) {
    const reset = String(value('quota.reset') ?? 'calendar_month');
    const period = t('vpn.reset_' + reset, { days: Number(value('quota.interval_days') ?? 30) });
    result.push({ label: t('vpn.limit_traffic'), value: `${bytes(quota)} · ${period}`,
      own: own('quota.bytes', 'quota.reset', 'quota.interval_days') });
    result.push({ label: t('vpn.limit_exhausted'), value: t('vpn.exhausted_' + String(value('quota.on_exhausted') ?? 'freeze')),
      own: own('quota.on_exhausted') });
  } else {
    result.push({ label: t('vpn.limit_traffic'), value: t('vpn.no_limit'), own: own('quota.bytes') });
  }

  const down = value('speed.down_kbit');
  const up = value('speed.up_kbit');
  const limited = (kbit: unknown): kbit is number => typeof kbit === 'number' && kbit > 0;
  result.push({ label: t('vpn.limit_speed'),
    value: limited(down) || limited(up)
      ? t('vpn.speed_value', { down: limited(down) ? speed(down) : '∞', up: limited(up) ? speed(up) : '∞' })
      : t('vpn.no_limit'),
    own: own('speed.down_kbit', 'speed.up_kbit') });

  if (value('connections.mode') === 'enforce')
    result.push({ label: t('vpn.limit_networks'), value: String(value('connections.max_sources') ?? 1),
      own: own('connections.mode', 'connections.max_sources') });
  return result;
}
