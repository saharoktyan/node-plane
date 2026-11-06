<!-- Node load: one small chart per node of the metric chosen, over the span
     chosen, with the latest value beside the node's name. Readings come every
     five minutes; the page asks again each minute, keeping the charts drawn
     while it waits. -->
<script lang="ts">
  import { api, call, type Schemas } from '$lib/api/client';
  import Line, { type Point, type Series } from '$lib/charts/Line.svelte';
  import { percent, rate } from '$lib/charts/scale';
  import { i18n, t } from '$lib/i18n/index.svelte';

  type Metrics = Schemas['NodeMetricsOutput'];
  type Reading = Schemas['NodeMetricPoint'];
  const RANGES = ['24h', '7d', '30d'] as const;
  const METRICS = ['cpu', 'ram', 'load', 'disk', 'net'] as const;
  type Metric = (typeof METRICS)[number];
  const SPAN = { '24h': 86_400_000, '7d': 7 * 86_400_000, '30d': 30 * 86_400_000 };
  const REFRESH = 60_000;

  let range = $state<(typeof RANGES)[number]>('24h');
  let metric = $state<Metric>('cpu');
  let data = $state<Metrics | null>(null);
  let failed = $state(false);

  async function fetchMetrics() {
    try {
      data = await call(api.GET('/api/v1/admin/metrics/nodes', { params: { query: { range } } }));
      failed = false;
    } catch {
      failed = !data;
    }
  }
  $effect(() => {
    void range;
    fetchMetrics();
    const timer = setInterval(fetchMetrics, REFRESH);
    return () => clearInterval(timer);
  });

  const number = (value: number) => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 2 }).format(value);
  const read: Record<Metric, { values: (point: Reading) => (number | null)[]; format: (value: number) => string;
    top: number | null }> = {
    cpu: { values: (point) => [point.cpu_percent ?? null], format: (value) => percent(value, i18n.language), top: 100 },
    ram: { values: (point) => [point.ram_percent ?? null], format: (value) => percent(value, i18n.language), top: 100 },
    disk: { values: (point) => [point.disk_free_percent ?? null], format: (value) => percent(value, i18n.language),
      top: 100 },
    load: { values: (point) => [point.load1 ?? null], format: number, top: null },
    net: { values: (point) => [point.net_rx_bps ?? null, point.net_tx_bps ?? null],
      format: (value) => rate(value, i18n.language), top: null }
  };
  const series = $derived<Series[]>(metric === 'net'
    ? [{ label: t('charts.received'), color: 'var(--series-1)' }, { label: t('charts.sent'), color: 'var(--series-2)' }]
    : [{ label: t('charts.metric_' + metric), color: 'var(--series-1)' }]);
  const end = $derived(data ? Date.now() : 0);
  const points = (node: Metrics['nodes'][number]): Point[] =>
    node.points.map((point) => ({ at: Date.parse(point.at), values: read[metric].values(point) }));
  function latest(node: Metrics['nodes'][number]): string {
    if (!node.latest) return t('charts.no_readings');
    const values = read[metric].values(node.latest);
    if (values.every((value) => value == null)) return '—';
    if (metric === 'load' && node.latest.cpus) return `${number(values[0]!)} / ${node.latest.cpus} CPU`;
    return values.map((value) => (value == null ? '—' : read[metric].format(value))).join(' · ');
  }
</script>

<section class="block">
  <div class="block-head">
    <h2>{t('admin.node_load')}</h2>
    <div class="choices" role="group" aria-label={t('charts.span')}>
      {#each RANGES as name (name)}
        <button aria-pressed={range === name} onclick={() => (range = name)}>{t('charts.range_' + name)}</button>
      {/each}
    </div>
  </div>
  <div class="filters">
    <div class="choices" role="group" aria-label={t('charts.metric')}>
      {#each METRICS as name (name)}
        <button aria-pressed={metric === name} onclick={() => (metric = name)}>{t('charts.metric_' + name)}</button>
      {/each}
    </div>
    {#if series.length > 1}
      <div class="legend">
        {#each series as line (line.label)}<span><i style:background={line.color}></i>{line.label}</span>{/each}
      </div>
    {/if}
  </div>
  {#if failed}
    <p class="error">{t('common.error')}</p>
  {:else if data}
    {#if !data.nodes.length}<p class="muted">{t('charts.no_nodes')}</p>{/if}
    <div class="grid" class:stale={data.range !== range}>
      {#each data.nodes as node (node.key)}
        <div class="node">
          <div class="head">
            <strong>{node.flag} {node.title}</strong>
            <span class="now">{latest(node)}</span>
          </div>
          {#if node.points.length}
            <Line points={points(node)} {series} start={end - SPAN[range]} {end} top={read[metric].top}
              gap={data.resolution === '5m' ? 15 * 60_000 : 3 * 3_600_000} format={read[metric].format}
              label={`${node.title}: ${t('charts.metric_' + metric)}`} height={130} />
          {:else}
            <p class="empty">{t('charts.no_readings_yet')}</p>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <p class="muted">{t('common.loading')}</p>
  {/if}
</section>

<style>
  .filters {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem 1.5rem;
    margin-top: 0.4rem;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 22rem), 1fr));
    gap: 1.5rem 2rem;
    margin-top: 1rem;
    transition: opacity 0.2s;
  }
  .grid.stale {
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
  .empty {
    display: grid;
    place-items: center;
    min-height: 130px;
    margin: 0.75rem 0 0;
    color: var(--muted);
    font-family: var(--serif);
    font-style: italic;
    text-align: center;
  }
</style>
