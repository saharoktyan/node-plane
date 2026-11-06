import { i18n } from '$lib/i18n/index.svelte';

export function date(value: string | null | undefined): string {
  if (!value) return '—';
  return new Date(value).toLocaleDateString(i18n.language, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—';
  return new Date(value).toLocaleString(i18n.language, { dateStyle: 'medium', timeStyle: 'short' });
}

export function bytes(value: number | null | undefined): string {
  if (value == null) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

export function key(): string {
  return crypto.randomUUID();
}
