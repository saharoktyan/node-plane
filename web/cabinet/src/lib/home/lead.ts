/** The home page's opening sentence: what matters about the account's
 * services right now, said once — VPN's term and traffic when there is VPN. */
import type { Schemas } from '$lib/api/client';
import { i18n, t } from '$lib/i18n/index.svelte';
import { bytes } from '$lib/ui/format';

type Profile = Schemas['ServiceProfileOutput'];

const day = (value: unknown) =>
  new Date(String(value)).toLocaleDateString(i18n.language, { day: 'numeric', month: 'long' });

export function lead(profiles: Profile[], name: string): string {
  const vpn = profiles.find((profile) => profile.service === 'vpn');
  if (!vpn) return profiles.length ? t('home.hello', { name }) : t('home.none');
  const summary = vpn.summary;
  const parts: string[] = [];
  if (vpn.status === 'frozen') parts.push(t('home.vpn_frozen'));
  else if (vpn.status === 'expired') parts.push(t('home.vpn_expired', { date: day(summary.expires_at) }));
  else if (vpn.status === 'suspended')
    parts.push(t('home.vpn_suspended', { date: day(summary.quota_reset_at) }));
  else {
    parts.push(summary.expires_at ? t('home.vpn_until', { date: day(summary.expires_at) }) : t('home.vpn_open'));
    if (summary.quota_limit_bytes != null)
      parts.push(t('home.vpn_used', { used: bytes(Number(summary.quota_used_bytes)),
        limit: bytes(Number(summary.quota_limit_bytes)), date: day(summary.quota_reset_at) }));
  }
  if (summary.renewal === 'pending') parts.push(t('home.vpn_renewal'));
  return parts.join(' ');
}

/** «1 сервис», «2 сервиса», «5 сервисов». */
export function count(value: number): string {
  const form = new Intl.PluralRules(i18n.language).select(value);
  return t(form === 'one' ? 'home.count_one' : form === 'few' ? 'home.count_few' : 'home.count_many', { count: value });
}

/** «сегодня», «вчера» or the date. */
export function when(value: string, now = new Date()): string {
  const then = new Date(value);
  const days = Math.round((new Date(now.toDateString()).getTime() - new Date(then.toDateString()).getTime()) / 86_400_000);
  return days === 0 ? t('home.today') : days === 1 ? t('home.yesterday')
    : then.toLocaleDateString(i18n.language, { day: 'numeric', month: 'short' });
}
