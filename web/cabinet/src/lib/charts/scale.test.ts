import { describe, expect, it } from 'vitest';
import { nearest, percent, rate, sample, size, sizeUnit, ticks, timeTicks } from './scale';

describe('chart scales', () => {
  it('rounds ticks up from zero', () => expect(ticks(73)).toEqual([0, 20, 40, 60, 80]));
  it('keeps a fixed top', () => expect(ticks(100)).toEqual([0, 25, 50, 75, 100]));
  it('counts in whole steps', () => expect(ticks(2, 4, 1, true)).toEqual([0, 1, 2]));
  it('has an axis for nothing', () => expect(ticks(0)).toEqual([0, 1]));
  it('rounds sizes in their unit', () => {
    const gib = 1024 ** 3;
    expect(sizeUnit(30 * gib)).toBe(gib);
    expect(ticks(30 * gib, 4, gib)).toEqual([0, 10, 20, 30].map((value) => value * gib));
  });
  it('names sizes and rates', () => {
    expect(size(1.5 * 1024 ** 3)).toBe('1.5 GiB');
    expect(size(20 * 1024 ** 2)).toBe('20 MiB');
    expect(size(0.5 * 1024 ** 3, 'en', 1024 ** 3)).toBe('0.5 GiB');
    expect(rate(12_400_000)).toBe('12 Mb/s');
    expect(rate(800)).toBe('800 bit/s');
    expect(percent(42.4)).toBe('42%');
  });
  it('puts day ticks on midnights', () => {
    const start = new Date(2026, 10, 1, 13, 20).getTime();
    const all = timeTicks(start, start + 7 * 86_400_000);
    expect(all).toHaveLength(7);
    expect(new Date(all[0]).getHours()).toBe(0);
  });
  it('finds the nearest point', () => {
    expect(nearest([0, 10, 20, 30], 14)).toBe(1);
    expect(nearest([0, 10, 20, 30], 99)).toBe(3);
  });
  it('samples evenly and keeps the last', () => {
    expect(sample([1, 2, 3, 4, 5, 6], 3)).toEqual([2, 4, 6]);
  });
});
