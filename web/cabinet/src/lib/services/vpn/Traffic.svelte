<!-- A member's traffic: how the quota fills over its period up to the limit,
     what went each day of the last month, received and sent, and through
     which server and protocol. -->
<script lang="ts">
  import { api, call, type Schemas } from '$lib/api/client';
  import Bars from '$lib/charts/Bars.svelte';
  import Columns, { type Column } from '$lib/charts/Columns.svelte';
  import Line, { type Point } from '$lib/charts/Line.svelte';
  import { size, sizeUnit } from '$lib/charts/scale';
  import { i18n, t } from '$lib/i18n/index.svelte';

  let { profileId, quota }: { profileId: string; quota: Record<string, unknown> | null | undefined } = $props();
  const DAY = 86_400_000;

  let history = $state<Schemas['MemberTrafficHistory'] | null>(null);
  let failed = $state(false);
  $effect(() => {
    call(api.GET('/api/v1/me/profiles/{profile_id}/history', { params: { path: { profile_id: profileId } } }))
      .then((value) => (history = value))
      .catch(() => (failed = true));
  });

  const bytes = (value: number) => size(value, i18n.language);
  const day = (value: string, options: Intl.DateTimeFormatOptions) =>
    new Date(value + 'T00:00:00').toLocaleDateString(i18n.language, options);
  const series = $derived([{ label: t('charts.received'), color: 'var(--series-1)' },
    { label: t('charts.sent'), color: 'var(--series-2)' }]);
  const days = $derived<Column[]>((history?.days ?? []).map((entry) => ({
    label: day(entry.day, { weekday: 'short', day: 'numeric', month: 'long' }),
    short: day(entry.day, { day: 'numeric', month: 'short' }),
    values: [entry.downlink_bytes, entry.uplink_bytes]
  })));
  const highest = $derived(Math.max(0,
    ...(history?.days ?? []).map((entry) => entry.uplink_bytes + entry.downlink_bytes)));

  /** The quota's use at the end of each day of its period, counted back from
   * what is used now; days before the first traffic are left out, since the
   * history may begin within the period. */
  const period = $derived.by(() => {
    if (!history || !quota?.period_start || !quota.period_end) return null;
    const start = Date.parse(String(quota.period_start));
    const end = Date.parse(String(quota.period_end));
    const inside = history.days.filter((entry) => Date.parse(entry.day + 'T23:59:59') >= start);
    let left = Number(quota.used_bytes);
    const points: Point[] = [];
    for (const entry of [...inside].reverse()) {
      const at = Math.min(Date.now(), Date.parse(entry.day + 'T00:00:00') + DAY);
      points.unshift({ at, values: [Math.max(0, left)] });
      left -= entry.uplink_bytes + entry.downlink_bytes;
    }
    const first = inside.findIndex((entry) => entry.uplink_bytes + entry.downlink_bytes > 0);
    return { start, end, points: first < 0 ? [] : points.slice(first), limit: Number(quota.limit_bytes) };
  });
</script>

<section class="block traffic">
  <div class="block-head"><h2>{t('vpn.traffic_history')}</h2></div>
  {#if failed}
    <p class="error">{t('common.error')}</p>
  {:else if history}
    <div class="grid">
      {#if period}
        <div>
          <h3>{t('vpn.quota_period')}</h3>
          <Line points={period.points} series={[{ label: t('vpn.quota_used'), color: 'var(--text)' }]}
            start={period.start} end={period.end} gap={2 * DAY} format={bytes} unit={sizeUnit(period.limit)}
            tick={(value) => size(value, i18n.language, sizeUnit(period.limit))}
            limit={{ value: period.limit, label: t('vpn.quota_limit', { limit: bytes(period.limit) }) }}
            label={t('vpn.quota_period')} />
        </div>
      {/if}
      <div>
        <div class="sub">
          <h3>{t('vpn.traffic_days')}</h3>
          <div class="legend">
            {#each series as line (line.label)}<span><i class="box" style:background={line.color}></i>{line.label}</span>{/each}
          </div>
        </div>
        <Columns columns={days} {series} format={bytes} unit={sizeUnit(highest)}
          tick={(value) => size(value, i18n.language, sizeUnit(highest))} label={t('vpn.traffic_days')}
          every={7} />
      </div>
      <div>
        <h3>{t('vpn.traffic_servers')}</h3>
        {#if history.servers.length}
          <Bars items={history.servers.map((server) => ({ label: `${server.flag} ${server.title}`.trim(),
            note: t('charts.protocol_' + server.protocol), value: server.bytes }))} format={bytes}
            label={t('vpn.traffic_servers')} />
        {:else}
          <p class="muted">{t('charts.no_traffic')}</p>
        {/if}
      </div>
    </div>
  {:else}
    <p class="muted">{t('common.loading')}</p>
  {/if}
</section>

<style>
  .traffic {
    margin-top: 2rem;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));
    gap: 0 2.5rem;
    margin-top: 1rem;
  }
  /* Titles in a row share their height, so the charts start on one line. */
  .grid > div {
    display: grid;
    grid-row: span 2;
    grid-template-rows: subgrid;
    align-content: start;
    padding-bottom: 1.5rem;
  }
  .sub {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.25rem 1rem;
  }
  h3 {
    margin: 0;
    font-size: 13px;
    font-weight: 500;
    color: var(--soft);
  }
</style>
