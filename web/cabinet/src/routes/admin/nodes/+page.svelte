<!-- Nodes: servers with an agent, and the services each runs. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';

  const load = call(api.GET('/api/v1/nodes', { params: { query: { stage: 'node', limit: 100 } } }));
  type Instance = { id: string; service: string; state: string };
</script>

<h1>{t('admin.nodes')}</h1>
<Async {load}>
  {#snippet children(page)}
    <div class="grid">
      {#each page.items as node (node.key)}
        <div class="card">
          <div class="row"><strong>{node.flag} {node.title}</strong>
            {#if !node.enabled}<span class="badge warn">{t('admin.disabled')}</span>{/if}</div>
          <div class="muted">{node.region}</div>
          <div class="row instances">
            {#each (node.service_instances ?? []) as Instance[] as instance (instance.id)}
              <span class={'badge ' + (instance.state === 'active' ? 'ok' : 'warn')}>
                {serviceName(instance.service)} · {t('admin.state_' + instance.state)}</span>
            {/each}
          </div>
        </div>
      {:else}
        <p class="muted">{t('admin.no_nodes')}</p>
      {/each}
    </div>
  {/snippet}
</Async>

<style>
  .instances {
    margin-top: 0.5rem;
  }
</style>
