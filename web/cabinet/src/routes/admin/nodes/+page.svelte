<!-- Nodes: servers with an agent, and the services each runs. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';

  const load = call(api.GET('/api/v1/nodes', { params: { query: { stage: 'node', limit: 100 } } }));
  type Instance = { id: string; service: string; state: string };
</script>

<div class="page-head"><h1>{t('admin.nodes')}</h1></div>
<Async {load}>
  {#snippet children(page)}
    <table>
      <thead><tr><th>{t('admin.node')}</th><th>{t('admin.region')}</th><th>{t('admin.services')}</th>
        <th>{t('admin.state')}</th></tr></thead>
      <tbody>
        {#each page.items as node (node.key)}
          <tr>
            <td>{node.flag} {node.title}</td>
            <td class="muted">{node.region}</td>
            <td>
              <div class="row">
                {#each (node.service_instances ?? []) as Instance[] as instance (instance.id)}
                  <span class={'badge ' + (instance.state === 'active' ? 'ok' : 'warn')}
                    title={t('admin.state_' + instance.state)}>{serviceName(instance.service)}</span>
                {/each}
              </div>
            </td>
            <td>{node.enabled ? (node.settings_apply_pending ? t('admin.state_changes_pending') : t('admin.state_active'))
              : t('admin.disabled')}</td>
          </tr>
        {:else}
          <tr><td colspan="4" class="muted">{t('admin.no_nodes')}</td></tr>
        {/each}
      </tbody>
    </table>
  {/snippet}
</Async>
