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

<div class="page-head"><h1>{t('admin.services')}</h1></div>
<Async {load}>
  {#snippet children(catalog)}
    {#each kinds as kind (kind)}
      {@const items = catalog.items.filter((item) => item.kind === kind)}
      {#if items.length || kind === 'user'}
      <section class="block">
        <h2>{t('admin.kind_' + kind)} <span class="muted hint">{t('admin.kind_hint_' + kind)}</span></h2>
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
      {:else}
        <p class="muted">{t('admin.manifests_later')}</p>
      {/if}
      </section>
      {/if}
    {/each}
  {/snippet}
</Async>

<style>
  .hint {
    font-weight: 400;
    font-size: 0.9rem;
    margin-left: 0.5rem;
  }
</style>
