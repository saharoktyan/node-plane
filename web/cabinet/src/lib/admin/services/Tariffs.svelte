<!-- Tariffs as the bot and Workstation list them: each one's term, the nodes
     it grants and how many profiles are on it. A new one starts with its
     name in each language and opens on its card for the rest. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import Localized from '$lib/admin/Localized.svelte';
  import { draftOf, inputOf, type Draft } from '$lib/admin/localized';
  import { i18n, t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { unfold } from '$lib/ui/motion';
  import { accessLabel, type Region, type Tariff } from './tariffs';

  let items = $state<Tariff[] | null>(null);
  let regions = $state<Region[]>([]);
  let languages = $state<string[]>(['en', 'ru']);
  let creating = $state<Draft | null>(null);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      const [list, found, system] = await Promise.all([call(api.GET('/api/v1/tariffs')),
        call(api.GET('/api/v1/regions', { params: { query: { limit: 100 } } })).catch(() => ({ items: [] })),
        call(api.GET('/api/v1/system/languages')).catch(() => null)]);
      items = list.items;
      regions = (found as { items: Region[] }).items;
      if (system) languages = system.languages;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  async function create() {
    if (!creating) return;
    const titles = inputOf(creating, languages);
    if (!titles) {
      failure = t('vpnadmin.title_needed');
      return;
    }
    busy = true;
    failure = '';
    try {
      const tariff = await call(api.POST('/api/v1/tariffs', { body: { titles } }));
      await goto('/admin/services/vpn/tariffs/' + tariff.id);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const nodes = (tariff: Tariff) => {
    const label = accessLabel(tariff.access_policy, regions);
    return 'key' in label ? t('vpnadmin.' + label.key) : label.names;
  };
</script>

<div class="head">
  <p class="muted small">{t('vpnadmin.tariffs_hint')}</p>
  {#if account.may('settings.manage') && !creating}
    <button class="primary" onclick={() => (creating = draftOf(null, languages, i18n.language))}>{t('vpnadmin.new_tariff')}</button>
  {/if}
</div>
{#if creating}
  <form class="create" transition:unfold onsubmit={(event) => { event.preventDefault(); create(); }}>
    <Localized bind:draft={creating} {languages} label={t('vpnadmin.tariff_title')} max={64} />
    <div class="row">
      <button class="primary" disabled={busy}>{t('vpnadmin.create')}</button>
      <button type="button" onclick={() => { creating = null; failure = ''; }}>{t('common.cancel')}</button>
    </div>
  </form>
{/if}
{#if failure}<p class="error">{failure}</p>{/if}

{#if items && !items.length}
  <p class="muted">{t('vpnadmin.no_tariffs')}</p>
{:else if items}
  <div class="table"><table>
    <thead><tr><th>{t('vpnadmin.tariff')}</th><th>{t('vpnadmin.term')}</th><th>{t('vpnadmin.nodes')}</th>
      <th class="count">{t('vpnadmin.profiles')}</th></tr></thead>
    <tbody>
      {#each items as tariff (tariff.id)}
        <tr class="link" onclick={() => goto('/admin/services/vpn/tariffs/' + tariff.id)}>
          <td><a href={'/admin/services/vpn/tariffs/' + tariff.id}>{tariff.title}</a></td>
          <td class="muted">{tariff.duration_days ? t('vpnadmin.days', { count: tariff.duration_days }) : t('vpnadmin.no_term')}</td>
          <td class="muted">{nodes(tariff)}</td>
          <td class="count">{tariff.profiles}</td>
        </tr>
      {/each}
    </tbody>
  </table></div>
{/if}

<style>
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }
  .head p {
    margin: 0;
  }
  .small {
    font-size: 13px;
  }
  .create {
    display: grid;
    gap: 0.9rem;
    max-width: 34rem;
    margin: 1rem 0;
  }
  .table {
    margin-top: 1rem;
  }
  .count {
    text-align: right;
  }
</style>
