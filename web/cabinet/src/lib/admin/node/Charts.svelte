<!-- A server's load beside its card: CPU, memory, network and disk over the
     span chosen, each with its latest reading, as the overview draws every
     server. Readings come every five minutes; it asks again each minute. -->
<script lang="ts">
  import { api, call, type Schemas } from '$lib/api/client';
  import Line, { type Point, type Series } from '$lib/charts/Line.svelte';
  import { percent, rate } from '$lib/charts/scale';
  import { i18n, t } from '$lib/i18n/index.svelte';

  type Reading = Schemas['NodeMetricPoint'];
  type Metrics = Schemas['NodeMetricsOutput'];
  const RANGES = ['24h', '7d', '30d'] as const;
  const SPAN = { '24h': 86_400_000, '7d': 7 * 86_400_000, '30d': 30 * 86_400_000 };

  let { node }: { node: string } = $props();
  let range = $state<(typeof RANGES)[number]>('24h');
  let data = $state<Metrics | null>(null);
  let failed = $state(false);

  async function load() {
    try {
      data = await call(api.GET('/api/v1/admin/metrics/nodes', { params: { query: { range } } }));
      failed = false;
    } catch {
      failed = !data;
    }
  }
  $effect(() => {
    void range;
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  });

  const mine = $derived(data?.nodes.find((item) => item.key === node) ?? null);
  const end = $derived(data ? Date.now() : 0);
  const share = (value: number) => percent(value, i18n.language);
  const speed = (value: number) => rate(value, i18n.language);
  const CHARTS: { metric: string; values: (point: Reading) => (number | null)[]; format: (value: number) => string;
    top: number | null; series: () => Series[] }[] = [
    { metric: 'cpu', values: (point) => [point.cpu_percent ?? null], format: share, top: 100,
      series: () => [{ label: t('charts.metric_cpu'), color: 'var(--series-1)' }] },
    { metric: 'ram', values: (point) => [point.ram_percent ?? null], format: share, top: 100,
      series: () => [{ label: t('charts.metric_ram'), color: 'var(--series-1)' }] },
    { metric: 'net', values: (point) => [point.net_rx_bps ?? null, point.net_tx_bps ?? null], format: speed, top: null,
      series: () => [{ label: t('charts.received'), color: 'var(--series-1)' }, { label: t('charts.sent'), color: 'var(--series-2)' }] },
    { metric: 'disk', values: (point) => [point.disk_free_percent ?? null], format: share, top: 100,
      series: () => [{ label: t('charts.metric_disk'), color: 'var(--series-1)' }] }
  ];
  const points = (chart: (typeof CHARTS)[number]): Point[] =>
    (mine?.points ?? []).map((point) => ({ at: Date.parse(point.at), values: chart.values(point) }));
  const latest = (chart: (typeof CHARTS)[number]) => {
    const values = mine?.latest ? chart.values(mine.latest) : [];
    return values.length && values.some((value) => value != null)
      ? values.map((value) => (value == null ? '—' : chart.format(value))).join(' · ') : '—';
  };
</script>

<section class="block">
  <div class="block-head">
    <h2>{t('nodes.load_title')}</h2>
    <div class="choices" role="group" aria-label={t('charts.span')}>
      {#each RANGES as name (name)}
        <button aria-pressed={range === name} onclick={() => (range = name)}>{t('charts.range_' + name)}</button>
      {/each}
    </div>
  </div>
  {#if failed}
    <p class="error">{t('common.error')}</p>
  {:else if data && !mine?.points.length}
    <p class="empty">{t('charts.no_readings_yet')}</p>
  {:else if data}
    <div class="charts" class:stale={data.range !== range}>
      {#each CHARTS as chart (chart.metric)}
        {@const series = chart.series()}
        <div>
          <div class="head">
            <strong>{t('charts.metric_' + chart.metric)}</strong>
            <span class="now">{latest(chart)}</span>
          </div>
          {#if series.length > 1}
            <div class="legend">{#each series as line (line.label)}<span><i style:background={line.color}></i>{line.label}</span>{/each}</div>
          {/if}
          <Line points={points(chart)} {series} start={end - SPAN[range]} {end} top={chart.top}
            gap={data.resolution === '5m' ? 15 * 60_000 : 3 * 3_600_000} format={chart.format}
            label={`${node}: ${t('charts.metric_' + chart.metric)}`} height={120} />
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .charts {
    display: grid;
    gap: 1.5rem;
    margin-top: 0.75rem;
    transition: opacity 0.2s;
  }
  .charts.stale {
    opacity: 0.6;
  }
  .head {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    align-items: baseline;
    border-bottom: 1px solid var(--line);
    padding-bottom: 0.35rem;
  }
  .now {
    font-family: var(--serif);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .legend {
    margin-top: 0.4rem;
  }
  .empty {
    color: var(--muted);
    font-family: var(--serif);
    font-style: italic;
  }
</style>
