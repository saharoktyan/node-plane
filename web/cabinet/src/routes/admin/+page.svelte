<!-- The administration's overview in three tabs: the nodes, VPN and the
     controller itself. The numbers refresh every few seconds while the page
     is open; charts come with the metrics history. -->
<script lang="ts">
  import { page } from '$app/state';
  import { api, call, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { date, dateTime } from '$lib/ui/format';
  import { tiles } from '$lib/ui/tiles';

  type Overview = Schemas['AdminOverviewOutput'];
  const TABS = ['general', 'vpn', 'controller'] as const;
  const REFRESH = 15_000;

  const tab = $derived(TABS.find((name) => name === page.url.searchParams.get('tab')) ?? 'general');

  const fetchOverview = () => call(api.GET('/api/v1/admin/overview'));
  const load = fetchOverview();
  // The latest answer; a failed refresh keeps the last numbers.
  let fresh = $state<Overview | null>(null);
  $effect(() => {
    const timer = setInterval(() => fetchOverview().then((value) => (fresh = value)).catch(() => {}), REFRESH);
    return () => clearInterval(timer);
  });

  type Item = { text: string; note?: string; href?: string; action?: string };
  function attention(overview: Overview): Item[] {
    const items: Item[] = overview.problem_nodes.map((node) => ({ text: `${node.flag} ${node.title}`,
      note: t('admin.node_problem'), href: '/admin/nodes', action: t('admin.open') }));
    if (overview.requests_enabled && overview.pending_requests)
      items.push({ text: t('admin.requests_wait', { count: overview.pending_requests }), href: '/admin/users',
        action: t('admin.review') });
    if (overview.tasks?.blocked) items.push({ text: t('admin.tasks_blocked', { count: overview.tasks.blocked }) });
    if (overview.alerts_enabled && overview.unacknowledged_alerts)
      items.push({ text: t('admin.alerts_open', { count: overview.unacknowledged_alerts }) });
    const updates = overview.updates as { available?: boolean; latest_version?: string | null } | undefined;
    if (updates?.available)
      items.push({ text: t('admin.update_ready', { version: updates.latest_version ?? '' }), href: '/admin/updates',
        action: t('admin.updates') });
    return items;
  }
</script>

<div class="page-head">
  <h1>{t('admin.overview')}</h1>
  <div class="actions"><span class="caps live">{t('admin.live')}</span></div>
</div>
<nav class="tabs" aria-label={t('admin.overview_tabs')}>
  {#each TABS as name (name)}
    <a href={name === 'general' ? '/admin' : `?tab=${name}`} data-sveltekit-replacestate data-sveltekit-noscroll
      aria-current={tab === name ? 'page' : undefined}>{t('admin.tab_' + name)}</a>
  {/each}
</nav>

<Async {load}>
  {#snippet children(first)}
    {@const overview = fresh ?? first}
    {@const updates = overview.updates as { available?: boolean; latest_version?: string | null;
      last_checked_at?: string | null } | undefined}
    {@const backup = overview.backup as { enabled?: boolean; last_job?: { status: string; created_at: string } | null }
      | undefined}
    {@const items = attention(overview)}

    {#if tab === 'general'}
      <div class="stats" use:tiles>
        <div>
          <div class="label">{t('admin.nodes')}</div>
          <div class="value">{overview.nodes_enabled}<small>&thinsp;/&thinsp;{overview.nodes_total}</small></div>
          <div class="note" class:warn={overview.problem_nodes.length}>
            {overview.problem_nodes.length ? t('admin.problems_count', { count: overview.problem_nodes.length })
              : t('admin.no_problems')}</div>
        </div>
        {#if overview.invitations_total}
          <div>
            <div class="label">{t('admin.invitations')}</div>
            <div class="value">{overview.invitations_total}</div>
          </div>
        {/if}
        {#if overview.alerts_enabled}
          <div>
            <div class="label">{t('admin.alerts')}</div>
            <div class="value">{overview.unacknowledged_alerts ?? 0}</div>
          </div>
        {/if}
        <div>
          <div class="label">{t('admin.tasks')}</div>
          <div class="value">{overview.tasks?.running ?? 0}</div>
          <div class="note">{t('admin.tasks_note', { queued: overview.tasks?.queued ?? 0, blocked: overview.tasks?.blocked ?? 0 })}</div>
        </div>
      </div>
      <div class="columns">
        <div class="main">
          <section class="block">
            <div class="block-head"><h2>{t('admin.node_load')}</h2></div>
            <p class="later">{t('admin.charts_later_nodes')}</p>
          </section>
        </div>
        {@render aside(items)}
      </div>
    {:else if tab === 'vpn'}
      <div class="stats" use:tiles>
        <div>
          <div class="label">{t('admin.profiles')}</div>
          <div class="value">{overview.profiles_total}</div>
          <div class="note">{t('admin.profiles_note', { active: overview.profiles_active, frozen: overview.profiles_frozen })}</div>
        </div>
        <div>
          <div class="label">{t('admin.requests')}</div>
          <div class="value">{overview.pending_requests}</div>
          <div class="note">{overview.requests_enabled ? t('admin.requests_note') : t('admin.requests_off')}</div>
        </div>
      </div>
      <div class="columns">
        <div class="main">
          <section class="block">
            <div class="block-head"><h2>{t('admin.vpn_activity')}</h2></div>
            <p class="later">{t('admin.charts_later_vpn')}</p>
          </section>
        </div>
        {@render aside(items.filter((item) => item.href === '/admin/users'))}
      </div>
    {:else}
      <div class="stats" use:tiles>
        <div>
          <div class="label">{t('admin.version_label')}</div>
          <div class="value">{overview.version}</div>
          <div class="note" class:warn={updates?.available}>
            {updates?.available ? t('admin.update_to', { version: updates.latest_version ?? '' }) : t('admin.latest')}</div>
        </div>
        <div>
          <div class="label">{t('admin.tasks')}</div>
          <div class="value">{overview.tasks?.running ?? 0}</div>
          <div class="note">{t('admin.tasks_note', { queued: overview.tasks?.queued ?? 0, blocked: overview.tasks?.blocked ?? 0 })}</div>
        </div>
        <div>
          <div class="label">{t('admin.backup')}</div>
          <div class="value">{backup?.last_job ? date(backup.last_job.created_at) : t('admin.backup_never')}</div>
          <div class="note">{backup?.enabled ? t('admin.backup_on') : t('admin.backup_off')}</div>
        </div>
        <div>
          <div class="label">{t('admin.updates_checked')}</div>
          <div class="value">{updates?.last_checked_at ? date(updates.last_checked_at) : t('admin.never')}</div>
          {#if overview.checked_at}<div class="note">{t('admin.checked', { time: dateTime(overview.checked_at) })}</div>{/if}
        </div>
      </div>
      <section class="block">
        <div class="block-head"><h2>{t('admin.host')}</h2></div>
        <p class="later">{t('admin.controller_later')}</p>
      </section>
    {/if}
  {/snippet}
</Async>

{#snippet aside(items: Item[])}
  <aside>
    <div class="block-head"><h2>{t('admin.attention')}</h2></div>
    {#if items.length}
      <div class="rows">
        {#each items as item (item.text)}
          <div>
            <span class="grow"><strong>{item.text}</strong>{#if item.note}<span class="muted">&nbsp;· {item.note}</span>{/if}</span>
            {#if item.href}<a href={item.href}>{item.action}</a>{/if}
          </div>
        {/each}
      </div>
    {:else}
      <p class="muted">{t('admin.no_problems')}</p>
    {/if}
  </aside>
{/snippet}

<style>
  .live {
    color: var(--accent);
  }
  .note.warn {
    color: var(--warn);
  }
  /* Where a chart will stand: the room it takes, said in words. */
  .later {
    display: grid;
    place-items: center;
    min-height: 14rem;
    margin: 0.75rem 0 0;
    padding: 1.5rem;
    border: 1px dashed var(--line);
    color: var(--muted);
    text-align: center;
    font-family: var(--serif);
    font-style: italic;
  }
</style>
