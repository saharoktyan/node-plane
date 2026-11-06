<!-- The administration's overview: the same numbers as the bot's admin
     panel, in one line; nodes with problems below. Charts come next. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { dateTime } from '$lib/ui/format';

  const load = call(api.GET('/api/v1/admin/overview'));
</script>

<Async {load}>
  {#snippet children(overview)}
    <div class="page-head">
      <h1>{t('admin.overview')}</h1>
      <span class="muted">{t('admin.version', { version: overview.version })} ·
        {t('admin.checked', { time: dateTime(overview.checked_at) })}</span>
    </div>
    <div class="stats">
      <div><div class="label">{t('admin.nodes')}</div><div class="value">{overview.nodes_total}</div>
        <div class="note">{t('admin.enabled', { count: overview.nodes_enabled })}</div></div>
      <div><div class="label">{t('admin.profiles')}</div><div class="value">{overview.profiles_total}</div>
        <div class="note">{t('admin.active', { count: overview.profiles_active })} ·
          {t('admin.frozen', { count: overview.profiles_frozen })}</div></div>
      <div><div class="label">{t('admin.requests')}</div><div class="value">{overview.pending_requests}</div></div>
      {#if overview.alerts_enabled}
        <div><div class="label">{t('admin.alerts')}</div><div class="value">{overview.unacknowledged_alerts ?? 0}</div></div>
      {/if}
    </div>
    <section class="block">
      <h2>{t('admin.problems')}</h2>
      {#if overview.problem_nodes.length}
        <div class="rows">
          {#each overview.problem_nodes as node (node.key)}
            <div><span class="grow">{node.flag} {node.title}</span><span class="muted">{node.region}</span></div>
          {/each}
        </div>
      {:else}
        <p class="muted">{t('admin.no_problems')}</p>
      {/if}
    </section>
  {/snippet}
</Async>
