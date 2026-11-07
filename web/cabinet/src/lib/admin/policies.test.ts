import { describe, expect, it } from 'vitest';
import { FIELDS, fromInput, shown, toInput } from './policies';

const field = (key: string) => FIELDS.find((item) => item.key === key)!;

describe('policy fields', () => {
  it('enters bytes in GiB and speeds in Mbit/s', () => {
    expect(fromInput(field('quota.bytes'), '100')).toBe(100 * 2 ** 30);
    expect(toInput(field('quota.bytes'), 100 * 2 ** 30)).toBe('100');
    expect(fromInput(field('speed.up_kbit'), '2,5')).toBe(2500);
    expect(toInput(field('speed.up_kbit'), 2500)).toBe('2.5');
  });

  it('takes 0 as an exemption where a key allows it', () => {
    expect(fromInput(field('speed.down_kbit'), '0')).toBe(0);
    expect(fromInput(field('quota.throttle_kbit'), '0')).toBeNull();
    expect(fromInput(field('quota.throttle_kbit'), '0.064')).toBe(64);
  });

  it('refuses what is out of range', () => {
    expect(fromInput(field('devices.max'), '0')).toBeNull();
    expect(fromInput(field('devices.max'), '1.5')).toBeNull();
    expect(fromInput(field('connections.mode'), 'on')).toBeNull();
    expect(fromInput(field('devices.max'), '')).toBeNull();
  });

  it('shows a field only with the value it depends on', () => {
    expect(shown(field('quota.interval_days'), { 'quota.reset': 'interval' })).toBe(true);
    expect(shown(field('quota.interval_days'), { 'quota.reset': 'calendar_month' })).toBe(false);
  });
});
