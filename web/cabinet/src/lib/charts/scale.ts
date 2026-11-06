/** Axes for the cabinet's charts: round ticks from zero, time ticks on
 * whole hours or days, and the units the values are read in. */

/** Ticks from zero to a round top at or above `max`, about `count` of them.
 * `unit` makes them round in that unit (1024 ** n for sizes). */
export function ticks(max: number, count = 4, unit = 1, whole = false): number[] {
  const top = Math.max(max, 0) / unit;
  if (!top) return [0, unit];
  const raw = top / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const found = [1, 2, 2.5, 5, 10].map((factor) => factor * magnitude).find((value) => value >= raw)!;
  const step = whole ? Math.max(1, Math.ceil(found)) : found;
  const out: number[] = [];
  for (let value = 0; value < top + step / 2; value += step) out.push(Number((value * unit).toPrecision(12)));
  if (out[out.length - 1] < max) out.push(out[out.length - 1] + step * unit);
  return out;
}

/** The power of 1024 sizes up to `max` are counted in. */
export function sizeUnit(max: number): number {
  let unit = 1;
  while (max >= unit * 1024 && unit < 1024 ** 4) unit *= 1024;
  return unit;
}

const SIZES = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];

/** A size for an axis or a label: no trailing zero, one decimal under ten.
 * An axis passes its unit so every tick is counted in it. */
export function size(value: number, language = 'en', unit = sizeUnit(Math.abs(value))): string {
  const amount = value / unit;
  const digits = amount && Math.abs(amount) < 10 && unit > 1 ? 1 : 0;
  return `${new Intl.NumberFormat(language, { maximumFractionDigits: digits }).format(amount)} ${
    SIZES[Math.round(Math.log(unit) / Math.log(1024))]}`;
}

const BITS = ['bit', 'kilobit', 'megabit', 'gigabit'];

/** A rate in bits per second, in the reader's units ("12 Мбит/с"). */
export function rate(value: number, language = 'en'): string {
  let step = 0;
  while (Math.abs(value) >= 1000 ** (step + 1) && step < BITS.length - 1) step += 1;
  const amount = value / 1000 ** step;
  return new Intl.NumberFormat(language, { style: 'unit', unit: `${BITS[step]}-per-second`,
    maximumFractionDigits: amount && Math.abs(amount) < 10 ? 1 : 0 }).format(amount);
}

export function percent(value: number, language = 'en'): string {
  return new Intl.NumberFormat(language, { style: 'unit', unit: 'percent', maximumFractionDigits: 0 }).format(value);
}

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/** Times for the axis of a span: every six hours for a day, every day for a
 * week, every week for longer; on local whole hours and midnights. */
export function timeTicks(start: number, end: number): number[] {
  const span = end - start;
  const step = span <= 2 * DAY ? 6 * HOUR : span <= 10 * DAY ? DAY : 7 * DAY;
  const first = new Date(start);
  if (step < DAY) first.setHours(Math.ceil((first.getHours() + first.getMinutes() / 60) / 6) * 6, 0, 0, 0);
  else first.setHours(24, 0, 0, 0);
  const out: number[] = [];
  for (let at = first.getTime(); at <= end; at += step) out.push(at);
  return out;
}

/** How an axis names a time: hours within a day, dates otherwise. */
export function timeLabel(at: number, span: number, language = 'en'): string {
  return new Date(at).toLocaleString(language, span <= 2 * DAY ? { hour: '2-digit', minute: '2-digit' }
    : { day: 'numeric', month: 'short' });
}

/** The index of the point nearest to a time; points are in time order. */
export function nearest(times: number[], at: number): number {
  let low = 0;
  let high = times.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (times[middle] < at) low = middle;
    else high = middle;
  }
  return Math.abs(times[high] - at) < Math.abs(times[low] - at) ? high : low;
}

/** Evenly spaced picks of at most `count` items, the last one kept. */
export function sample<T>(items: T[], count: number): T[] {
  if (items.length <= count) return items;
  const step = items.length / count;
  return Array.from({ length: count }, (_, index) => items[Math.min(items.length - 1,
    Math.round((index + 1) * step) - 1)]);
}
