<!-- The administration's overview: the same numbers as the bot's admin panel. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { dateTime } from '$lib/ui/format';

  const load = call(api.GET('/api/v1/admin/overview'));
</script>

<h1>{t('admin.overview')}</h1>
<Async {load}>
  {#snippet children(overview)}
    <div class="grid">
      <div class="card"><div class="muted">{t('admin.nodes')}</div>
        <strong>{overview.nodes_enabled} / {overview.nodes_total}</strong></div>
      <div class="card"><div class="muted">{t('admin.profiles')}</div>
        <strong>{overview.profiles_active} / {overview.profiles_total}</strong>
        {#if overview.profiles_frozen}<div class="muted">{t('admin.frozen', { count: overview.profiles_frozen })}</div>{/if}
      </div>
      <div class="card"><div class="muted">{t('admin.requests')}</div><strong>{overview.pending_requests}</strong></div>
      {#if overview.alerts_enabled}
        <div class="card"><div class="muted">{t('admin.alerts')}</div><strong>{overview.unacknowledged_alerts ?? 0}</strong></div>
      {/if}
    </div>
    {#if overview.problem_nodes.length}
      <h2>{t('admin.problems')}</h2>
      <ul>
        {#each overview.problem_nodes as node (node.key)}<li>{node.flag} {node.title}</li>{/each}
      </ul>
    {/if}
    <p class="muted">{t('admin.version', { version: overview.version })} · {t('admin.checked', { time: dateTime(overview.checked_at) })}</p>
  {/snippet}
</Async>
