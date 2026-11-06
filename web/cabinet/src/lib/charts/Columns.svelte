<!-- Amounts per period as columns from one baseline, a surface gap between
     touching parts, the tops rounded. Two series stack, or stand side by side
     when they are not parts of one whole. Each
     column shows its numbers on hover or focus; a table says them to screen
     readers. -->
<script lang="ts" module>
  export type Column = { label: string; short: string; values: number[]; details?: { label: string; value: string }[] };
</script>

<script lang="ts">
  import type { Series } from './Line.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import { ticks } from './scale';

  let { columns, series, format, tick = format, unit = 1, whole = false, label, height = 150, every = 1,
    grouped = false }: {
    columns: Column[];
    series: Series[];
    format: (value: number) => string;
    /** How the axis names its ticks; the values' format by default. */
    tick?: (value: number) => string;
    unit?: number;
    /** Counts: ticks in whole steps. */
    whole?: boolean;
    label: string;
    height?: number;
    /** Name every n-th column under the axis. */
    every?: number;
    /** The series side by side instead of stacked. */
    grouped?: boolean;
  } = $props();

  const LEFT = 64;
  const RIGHT = 4;
  const TOP = 10;
  const BOTTOM = 22;
  const GAP = 2;
  let width = $state(0);
  let picked = $state<number | null>(null);
  let tipWidth = $state(160);

  const totals = $derived(columns.map((column) => column.values.reduce((sum, value) => sum + value, 0)));
  const tallest = $derived(grouped ? Math.max(0, ...columns.flatMap((column) => column.values))
    : Math.max(0, ...totals));
  const axis = $derived(ticks(tallest, 4, unit, whole));
  const ceiling = $derived(axis[axis.length - 1] || 1);
  const band = $derived(Math.max(1, (width - LEFT - RIGHT) / Math.max(1, columns.length)));
  const thick = $derived(Math.max(2, Math.min(24 * (grouped ? series.length : 1), band * 0.62)));
  const inner = $derived(height - TOP - BOTTOM);
  const y = (value: number) => TOP + (1 - value / ceiling) * inner;
  const left = (index: number) => LEFT + index * band + (band - thick) / 2;

  /** A part of a column; the top part has rounded corners. */
  function part(x: number, top: number, bottom: number, round: boolean, wide = thick): string {
    const radius = round ? Math.min(4, wide / 2, bottom - top) : 0;
    return `M${x},${bottom}V${top + radius}Q${x},${top} ${x + radius},${top}H${x + wide - radius}` +
      `Q${x + wide},${top} ${x + wide},${top + radius}V${bottom}Z`;
  }
  function parts(index: number) {
    const out: { d: string; color: string }[] = [];
    if (grouped) {
      const wide = (thick - GAP * (series.length - 1)) / series.length;
      columns[index].values.forEach((value, at) => {
        if (value > 0) out.push({ d: part(left(index) + at * (wide + GAP), Math.min(y(value), y(0) - 1), y(0), true,
          wide), color: series[at]?.color ?? series[0].color });
      });
      return out;
    }
    let base = 0;
    const values = columns[index].values;
    const lastShown = values.findLastIndex((value) => value > 0);
    values.forEach((value, at) => {
      if (value <= 0) return;
      const bottom = y(base) - (base ? GAP : 0);
      base += value;
      const top = Math.min(y(base), bottom - 1);
      out.push({ d: part(left(index), top, bottom, at === lastShown), color: series[at]?.color ?? series[0].color });
    });
    return out;
  }
  function key(event: KeyboardEvent) {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const step = event.key === 'ArrowLeft' ? -1 : 1;
      picked = Math.max(0, Math.min(columns.length - 1, (picked ?? (step < 0 ? columns.length : -1)) + step));
    } else if (event.key === 'Escape') picked = null;
  }
  const at = $derived(picked != null ? columns[picked] : null);
  // What a screen reader says for the picked column.
  const spoken = $derived.by(() => {
    const column = at ?? columns[columns.length - 1];
    if (!column) return '';
    return `${column.label}: ` + (series.length > 1 ? series.map((line, index) =>
      `${line.label} ${format(column.values[index] ?? 0)}`).join(', ') : format(column.values.reduce((a, b) => a + b, 0)));
  });
</script>

<div class="chart" bind:clientWidth={width}>
  {#if width}
    <svg {width} {height} role="slider" aria-roledescription={t('charts.chart')} aria-label={label}
      aria-valuemin={0} aria-valuemax={Math.max(0, columns.length - 1)} aria-valuenow={picked ?? columns.length - 1}
      aria-valuetext={spoken} tabindex="0" onkeydown={key} onblur={() => (picked = null)}
      onpointerleave={() => (picked = null)}>
      {#each axis as value (value)}
        <line class="grid" x1={LEFT} x2={width - RIGHT} y1={y(value)} y2={y(value)} />
        <text class="tick" x={LEFT - 8} y={y(value)} dy="0.32em" text-anchor="end">{tick(value)}</text>
      {/each}
      {#each columns as column, index (column.label)}
        <g class:dim={picked != null && picked !== index}>
          {#each parts(index) as piece, at (at)}<path d={piece.d} fill={piece.color} />{/each}
        </g>
        {#if index % every === (columns.length - 1) % every}
          <text class="tick" x={left(index) + thick / 2} y={height - 6} text-anchor="middle">{column.short}</text>
        {/if}
        <!-- The whole band answers the pointer, not only the painted part. -->
        <rect role="presentation" x={LEFT + index * band} y={TOP} width={band} height={inner} fill="transparent"
          onpointerenter={() => (picked = index)} />
      {/each}
      <line class="base" x1={LEFT} x2={width - RIGHT} y1={y(0)} y2={y(0)} />
    </svg>
    {#if at && picked != null}
      <div class="tip" bind:clientWidth={tipWidth}
        style:left={`${Math.min(Math.max(left(picked) + thick / 2, tipWidth / 2), width - tipWidth / 2)}px`}
        aria-hidden="true">
        <div class="when">{at.label}</div>
        {#if series.length > 1}
          {#each series as line, index (line.label)}
            <div class="row"><span class="key" style:background={line.color}></span>
              <strong>{format(at.values[index] ?? 0)}</strong><span>{line.label}</span></div>
          {/each}
        {:else}
          <div class="row"><strong>{format(totals[picked])}</strong></div>
        {/if}
        {#each at.details ?? [] as detail (detail.label)}
          <div class="row"><span>{detail.label}</span><span class="value">{detail.value}</span></div>
        {/each}
      </div>
    {/if}
  {/if}
  <div class="sr"><table>
    <caption>{label}</caption>
    <thead><tr><th></th>{#each series as line (line.label)}<th>{line.label}</th>{/each}</tr></thead>
    <tbody>
      {#each columns as column (column.label)}
        <tr><th>{column.label}</th>{#each series as line, index (line.label)}<td>{format(column.values[index] ?? 0)}</td>{/each}</tr>
      {/each}
    </tbody>
  </table></div>
</div>

<style>
  g {
    transition: opacity 0.15s;
  }
  g.dim {
    opacity: 0.45;
  }
  .tip .value {
    margin-left: auto;
    padding-left: 1rem;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
</style>
