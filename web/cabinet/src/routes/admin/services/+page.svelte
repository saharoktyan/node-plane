<!-- Services by kind: system ones, built-in VPN, and your own from
     manifests (stage I: manifests, the editor and test runs go here). -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';

  const load = call(api.GET('/api/v1/services'));
  const kinds = ['system', 'builtin', 'user'] as const;
</script>

<h1>{t('admin.services')}</h1>
<Async {load}>
  {#snippet children(catalog)}
    {#each kinds as kind (kind)}
      {@const items = catalog.items.filter((item) => item.kind === kind)}
      {#if items.length || kind === 'user'}
        <h2>{t('admin.kind_' + kind)}</h2>
        <p class="muted">{t('admin.kind_hint_' + kind)}</p>
      {/if}
      {#if items.length}
        <table>
          <thead><tr><th>{t('admin.service')}</th><th>{t('admin.state')}</th><th>{t('admin.instances')}</th></tr></thead>
          <tbody>
            {#each items as item (item.service)}
              <tr>
                <td>{serviceName(item.service)}</td>
                <td>{item.state ? t('admin.state_' + item.state) : '—'}</td>
                <td>{item.instances ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else if kind === 'user'}
        <p class="muted">{t('admin.manifests_later')}</p>
      {/if}
    {/each}
  {/snippet}
</Async>
