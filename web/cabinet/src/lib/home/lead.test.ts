import { beforeEach, describe, expect, it } from 'vitest';
import type { Schemas } from '$lib/api/client';
import { i18n } from '$lib/i18n/index.svelte';
import { count, lead, when } from './lead';

const vpn = (status: string, summary: Record<string, string | number | null>): Schemas['ServiceProfileOutput'] =>
  ({ id: 'p', service: 'vpn', kind: 'builtin', status, actions: [], summary });

describe('the home page lead', () => {
  beforeEach(() => i18n.set('ru'));

  it('tells the term and the traffic of a working VPN', () => {
    expect(lead([vpn('active', { expires_at: '2026-12-06T00:00:00Z', quota_used_bytes: 32212254720,
      quota_limit_bytes: 107374182400, quota_reset_at: '2026-12-01T00:00:00Z' })], 'x'))
      .toBe('VPN работает до 6 декабря. Израсходовано 30.0 GiB из 100.0 GiB, сброс 1 декабря.');
  });

  it('says when a paused VPN comes back', () => {
    expect(lead([vpn('suspended', { quota_reset_at: '2026-12-01T00:00:00Z' })], 'x')).toContain('вернётся 1 декабря');
  });

  it('greets an account without VPN and invites one without services', () => {
    expect(lead([{ ...vpn('active', {}), service: 'vaultwarden' }], 'Саша')).toBe('Здравствуйте, Саша.');
    expect(lead([], 'x')).toContain('Пока нет');
  });

  it('counts services and dates messages in words', () => {
    expect([count(1), count(2), count(5)]).toEqual(['1 сервис', '2 сервиса', '5 сервисов']);
    expect(when('2026-11-06T10:00:00', new Date('2026-11-06T20:00:00'))).toBe('сегодня');
    expect(when('2026-11-05T10:00:00', new Date('2026-11-06T20:00:00'))).toBe('вчера');
  });
});
