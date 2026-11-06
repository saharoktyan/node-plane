<!-- Values over time: one or two lines on one axis from zero, a wash under a
     lone line, a dot at each line's end. A gap in the readings breaks the
     line. The pointer or the arrow keys pick a time, and every line's value
     at it shows; a table says the same to screen readers. -->
<script lang="ts" module>
  export type Series = { label: string; color: string };
  export type Point = { at: number; values: (number | null)[] };
</script>

<script lang="ts">
  import { i18n, t } from '$lib/i18n/index.svelte';
  import { nearest, sample, ticks, timeLabel, timeTicks } from './scale';

  let { points, series, start, end, top = null, gap, format, tick = format, unit = 1, limit = null, label,
    height = 150 }: {
    points: Point[];
    series: Series[];
    start: number;
    end: number;
    /** A fixed top for the axis, such as 100 for percents. */
    top?: number | null;
    /** A longer pause between readings breaks the line. */
    gap: number;
    format: (value: number) => string;
    /** How the axis names its ticks; the values' format by default. */
    tick?: (value: number) => string;
    /** The unit ticks are round in. */
    unit?: number;
    /** A level the values should stay under, drawn across. */
    limit?: { value: number; label: string } | null;
    label: string;
    height?: number;
  } = $props();

  const LEFT = 64;
  const RIGHT = 10;
  const TOP = 10;
  const BOTTOM = 22;
  let width = $state(0);
  let picked = $state<number | null>(null);
  let tipWidth = $state(160);

  const highest = $derived(Math.max(limit?.value ?? 0,
    ...points.flatMap((point) => point.values.filter((value): value is number => value != null))));
  const axis = $derived(top != null ? ticks(top) : ticks(highest, 4, unit));
  const ceiling = $derived(axis[axis.length - 1] || 1);
  const plot = $derived(Math.max(1, width - LEFT - RIGHT));
  const x = (at: number) => LEFT + ((at - start) / Math.max(1, end - start)) * plot;
  const y = (value: number) => TOP + (1 - Math.min(value, ceiling) / ceiling) * (height - TOP - BOTTOM);
  const times = $derived(points.map((point) => point.at));

  /** Each line's path, broken where a value is missing or readings pause. */
  function path(index: number): string {
    let out = '';
    let previous: number | null = null;
    for (const point of points) {
      const value = point.values[index];
      if (value == null) {
        previous = null;
        continue;
      }
      const join = previous != null && point.at - previous <= gap;
      out += `${join ? 'L' : 'M'}${x(point.at).toFixed(1)},${y(value).toFixed(1)}`;
      previous = point.at;
    }
    return out;
  }
  /** The wash under a lone line, closed down to the baseline per piece. */
  function wash(): string {
    return path(0).split('M').filter(Boolean).map((piece) => {
      const coords = piece.split('L');
      const first = coords[0].split(',')[0];
      const last = coords[coords.length - 1].split(',')[0];
      return `M${first},${y(0)}L${piece}L${last},${y(0)}Z`;
    }).join('');
  }
  /** Time ticks far enough apart for their labels. */
  function spaced(): number[] {
    const room = end - start <= 2 * 86_400_000 ? 44 : 64;
    const out: number[] = [];
    for (const tick of timeTicks(start, end))
      if (!out.length || x(tick) - x(out[out.length - 1]) >= room) out.push(tick);
    return out;
  }
  const last = (index: number) => [...points].reverse().find((point) => point.values[index] != null);

  function pick(event: PointerEvent) {
    const box = (event.currentTarget as SVGElement).getBoundingClientRect();
    if (!times.length) return;
    picked = nearest(times, start + ((event.clientX - box.left - LEFT) / plot) * (end - start));
  }
  function key(event: KeyboardEvent) {
    if (!points.length) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const step = event.key === 'ArrowLeft' ? -1 : 1;
      picked = Math.max(0, Math.min(points.length - 1, (picked ?? (step < 0 ? points.length : -1)) + step));
    } else if (event.key === 'Escape') picked = null;
  }
  const at = $derived(picked != null ? points[picked] : null);
  // What a screen reader says for the picked time.
  const spoken = $derived.by(() => {
    const point = at ?? points[points.length - 1];
    if (!point) return '';
    return `${when(point.at)}: ` + series.map((line, index) =>
      `${line.label} ${point.values[index] == null ? '—' : format(point.values[index]!)}`).join(', ');
  });
  const when = (time: number) => new Date(time).toLocaleString(i18n.language,
    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
</script>

<div class="chart" bind:clientWidth={width}>
  {#if width}
    <svg {width} {height} role="slider" aria-roledescription={t('charts.chart')} aria-label={label}
      aria-valuemin={0} aria-valuemax={Math.max(0, points.length - 1)} aria-valuenow={picked ?? points.length - 1}
      aria-valuetext={spoken} tabindex="0" onpointermove={pick}
      onpointerleave={() => (picked = null)} onkeydown={key} onblur={() => (picked = null)}>
      {#each axis as value (value)}
        <line class="grid" x1={LEFT} x2={width - RIGHT} y1={y(value)} y2={y(value)} />
        <text class="tick" x={LEFT - 8} y={y(value)} dy="0.32em" text-anchor="end">{tick(value)}</text>
      {/each}
      {#each spaced() as time (time)}
        <text class="tick" x={x(time)} y={height - 6} text-anchor="middle">{timeLabel(time, end - start, i18n.language)}</text>
      {/each}
      <line class="base" x1={LEFT} x2={width - RIGHT} y1={y(0)} y2={y(0)} />
      {#if limit}
        <line class="limit" x1={LEFT} x2={width - RIGHT} y1={y(limit.value)} y2={y(limit.value)} />
        <text class="tick" x={width - RIGHT} y={y(limit.value) - 5} text-anchor="end">{limit.label}</text>
      {/if}
      {#if series.length === 1}<path d={wash()} fill={series[0].color} opacity="0.1" />{/if}
      {#each series as line, index (line.label)}
        <path d={path(index)} fill="none" stroke={line.color} stroke-width="2" stroke-linejoin="round"
          stroke-linecap="round" />
        {@const tail = last(index)}
        {#if tail && picked == null}
          <circle class="dot" cx={x(tail.at)} cy={y(tail.values[index]!)} r="4" fill={line.color} />
        {/if}
      {/each}
      {#if at}
        <line class="cross" x1={x(at.at)} x2={x(at.at)} y1={TOP} y2={y(0)} />
        {#each series as line, index (line.label)}
          {#if at.values[index] != null}
            <circle class="dot" cx={x(at.at)} cy={y(at.values[index]!)} r="4" fill={line.color} />
          {/if}
        {/each}
      {/if}
    </svg>
    {#if at}
      <div class="tip" bind:clientWidth={tipWidth}
        style:left={`${Math.min(Math.max(x(at.at), tipWidth / 2), width - tipWidth / 2)}px`} aria-hidden="true">
        <div class="when">{when(at.at)}</div>
        {#each series as line, index (line.label)}
          <div class="row"><span class="key" style:background={line.color}></span>
            <strong>{at.values[index] == null ? '—' : format(at.values[index]!)}</strong>
            {#if series.length > 1}<span>{line.label}</span>{/if}</div>
        {/each}
      </div>
    {/if}
  {/if}
  <div class="sr"><table>
    <caption>{label}</caption>
    <thead><tr><th></th>{#each series as line (line.label)}<th>{line.label}</th>{/each}</tr></thead>
    <tbody>
      {#each sample(points, 48) as point (point.at)}
        <tr><th>{when(point.at)}</th>{#each point.values as value, index (index)}<td>{value == null ? '—' : format(value)}</td>{/each}</tr>
      {/each}
    </tbody>
  </table></div>
</div>

<style>
  .limit {
    stroke: var(--accent);
    stroke-width: 1;
  }
</style>
