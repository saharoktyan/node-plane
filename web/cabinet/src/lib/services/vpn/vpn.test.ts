import { beforeEach, describe, expect, it } from 'vitest';
import { i18n } from '$lib/i18n/index.svelte';
import { lines, speed } from './limits';
import { qr } from './qr';

describe('what the tariff includes', () => {
  beforeEach(() => i18n.set('en'));

  it('says each limit and marks those set on the profile', () => {
    const result = lines([
      { key: 'devices.max', value: 3, own: false }, { key: 'quota.bytes', value: 107374182400, own: false },
      { key: 'quota.reset', value: 'interval', own: true }, { key: 'quota.interval_days', value: 14, own: false },
      { key: 'quota.on_exhausted', value: 'throttle', own: false }, { key: 'speed.down_kbit', value: 50000, own: true },
      { key: 'speed.up_kbit', value: null, own: false }, { key: 'connections.mode', value: 'off', own: false }
    ]);
    expect(result.map((line) => [line.label, line.value, line.own])).toEqual([
      ['Devices', '3', false],
      ['Traffic', '100.0 GiB · every 14 days', true],
      ['When the traffic runs out', 'the speed drops until the reset', false],
      ['Speed per device', '↓ 50 Mbit/s · ↑ ∞', true]
    ]);
  });

  it('a limit not in force reads as none; a zero exempts', () => {
    const result = lines([{ key: 'quota.bytes', value: 0, own: true }, { key: 'connections.mode', value: 'enforce', own: false },
      { key: 'connections.max_sources', value: 2, own: false }]);
    expect(result.map((line) => line.value)).toEqual(['no limit', 'no limit', 'no limit', '2']);
    expect(result[1].own).toBe(true);
    expect(speed(512)).toBe('512 kbit/s');
    expect(speed(1500)).toBe('1.5 Mbit/s');
  });
});

describe('a configuration as a QR code', () => {
  it('draws a link and the AmneziaVPN key without its scheme', () => {
    expect(qr('xray', 'tcp', 'vless://id@host:443?type=tcp#name')).toMatch(/^<svg/);
    const key = qr('awg', 'vpn', 'vpn://AAAA');
    expect(key).toEqual(qr('xray', 'tcp', 'AAAA'));
  });

  it('gives none for a text too long for one', () => {
    expect(qr('awg', 'conf', 'x'.repeat(2600))).toBeNull();
  });
});
