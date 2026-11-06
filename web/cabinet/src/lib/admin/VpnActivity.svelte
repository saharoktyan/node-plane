<!-- VPN's activity: traffic per day over a month, who is online on which
     server, the month's protocols, profiles ending in the coming weeks, new
     profiles and access requests per week, and the month's heaviest users
     in a table. -->
<script lang="ts">
  import type { Schemas } from '$lib/api/client';
  import Bars from '$lib/charts/Bars.svelte';
  import Columns, { type Column } from '$lib/charts/Columns.svelte';
  import { size, sizeUnit } from '$lib/charts/scale';
  import { i18n, t } from '$lib/i18n/index.svelte';

  let { activity }: { activity: Schemas['VpnActivityOutput'] } = $props();

  const bytes = (value: number) => size(value, i18n.language);
  const day = (value: string, options: Intl.DateTimeFormatOptions) =>
    new Date(value + 'T00:00:00').toLocaleDateString(i18n.language, options);
  const days = $derived<Column[]>(activity.days.map((entry) => ({
    label: day(entry.day, { weekday: 'short', day: 'numeric', month: 'long' }),
    short: day(entry.day, { day: 'numeric', month: 'short' }),
    values: [entry.bytes],
    // The day's biggest servers; the rest together.
    details: (() => {
      const sorted = Object.entries(entry.nodes).sort((a, b) => b[1] - a[1]);
      const shown = sorted.slice(0, 3).map(([key, value]) =>
        ({ label: activity.node_titles[key] ?? key, value: bytes(value) }));
      const rest = sorted.slice(3).reduce((sum, [, value]) => sum + value, 0);
      return rest ? [...shown, { label: t('charts.other'), value: bytes(rest) }] : shown;
    })()
  })));
  const weeks = $derived<Column[]>(activity.expiring.weeks.map((count, index) => ({
    label: t('charts.week_' + index), short: t('charts.week_short_' + index), values: [count] })));
  const busiest = $derived(sizeUnit(Math.max(0, ...activity.days.map((entry) => entry.bytes))));
  const week = (value: string) => day(value, { day: 'numeric', month: 'short' });
  const created = $derived<Column[]>(activity.weeks.map((entry) => ({
    label: t('charts.week_of', { date: week(entry.week) }), short: week(entry.week),
    values: [entry.profiles_created] })));
  const requests = $derived<Column[]>(activity.weeks.map((entry) => ({
    label: t('charts.week_of', { date: week(entry.week) }), short: week(entry.week),
    values: [entry.requests_created, entry.requests_decided] })));
  const requestSeries = $derived([{ label: t('charts.requests_came'), color: 'var(--series-1)' },
    { label: t('charts.requests_decided'), color: 'var(--series-2)' }]);
  const count = (value: number) => new Intl.NumberFormat(i18n.language).format(value);
</script>

<section class="block">
  <div class="block-head"><h2>{t('charts.traffic_days')}</h2></div>
  <Columns columns={days} series={[{ label: t('charts.traffic'), color: 'var(--series-1)' }]} format={bytes}
    unit={busiest} tick={(value) => size(value, i18n.language, busiest)} label={t('charts.traffic_days')}
    every={7} />
</section>

<div class="pair">
  <section class="block">
    <div class="block-head"><h2>{t('charts.online_servers')}</h2></div>
    {#if activity.online.nodes.length}
      <Bars items={activity.online.nodes.map((node) => ({ label: `${node.flag} ${node.title}`, value: node.devices }))}
        format={(value) => t('charts.devices_count', { count: value })} label={t('charts.online_servers')} />
    {:else}
      <p class="muted">{t('charts.nobody_online')}</p>
    {/if}
  </section>
  <section class="block">
    <div class="block-head"><h2>{t('charts.protocols_month')}</h2></div>
    <Bars items={Object.entries(activity.protocols).map(([name, value]) => ({ label: t('charts.protocol_' + name),
      value, note: t('charts.online_count', { count: activity.online.protocols[name] ?? 0 }) }))}
      format={bytes} label={t('charts.protocols_month')} />
  </section>
  <section class="block">
    <div class="block-head">
      <h2>{t('charts.expiring')}</h2>
      {#if activity.expiring.renewals_pending}
        <a href="/admin/users">{t('charts.renewals_pending', { count: activity.expiring.renewals_pending })}</a>
      {/if}
    </div>
    <Columns columns={weeks} series={[{ label: t('charts.profiles'), color: 'var(--series-1)' }]} format={count} whole
      label={t('charts.expiring')} height={130} />
  </section>
  <section class="block">
    <div class="block-head"><h2>{t('charts.profiles_weeks')}</h2></div>
    <Columns columns={created} series={[{ label: t('charts.profiles'), color: 'var(--series-1)' }]} format={count}
      whole label={t('charts.profiles_weeks')} height={130} every={3} />
  </section>
  <section class="block">
    <div class="block-head">
      <h2>{t('charts.requests_weeks')}</h2>
      <div class="legend">
        {#each requestSeries as line (line.label)}<span><i class="box" style:background={line.color}></i>{line.label}</span>{/each}
      </div>
    </div>
    <Columns columns={requests} series={requestSeries} format={count} whole grouped
      label={t('charts.requests_weeks')} height={130} every={3} />
  </section>
</div>

<section class="block top">
  <div class="block-head"><h2>{t('charts.top')}</h2></div>
  {#if activity.top.length}
    <div class="table"><table>
      <thead><tr><th>{t('charts.profile')}</th><th class="number">{t('charts.traffic')}</th>
        <th class="number">{t('charts.devices')}</th></tr></thead>
      <tbody>
        {#each activity.top as profile (profile.profile_id)}
          <tr><td>{profile.display_name}</td><td class="number">{bytes(profile.bytes)}</td>
            <td class="number">{profile.devices}</td></tr>
        {/each}
      </tbody>
    </table></div>
  {:else}
    <p class="muted">{t('charts.no_traffic')}</p>
  {/if}
</section>

<style>
  .block + .pair {
    margin-top: 2rem;
  }
  .top {
    margin-top: 2rem;
  }
  .number {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  /* Heads in a row share their height, so the charts start on one line. */
  .pair {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr));
    gap: 0 2.5rem;
  }
  .pair > section {
    display: grid;
    grid-row: span 2;
    grid-template-rows: subgrid;
    align-content: start;
    padding-bottom: 2rem;
  }
</style>
