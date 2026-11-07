<!-- A tariff's card, as the bot and Workstation edit it: its name and
     description in each language, its term, the nodes it grants, and its
     own limits. One with profiles on it cannot be deleted. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import Localized from '$lib/admin/Localized.svelte';
  import { draftOf, empty, inputOf, type Draft } from '$lib/admin/localized';
  import Policies from '$lib/admin/Policies.svelte';
  import { TERMS, accessLabel, accessOf, chosenRegions, everyNode, keepsNodes, type Region, type Tariff } from '$lib/admin/services/tariffs';
  import { i18n, t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';

  type Form = { titles: Draft; descriptions: Draft; term: string; mode: 'keep' | 'all' | 'regions'; regions: string[] };
  const id = $derived(page.params.id as string);
  let tariff = $state<Tariff | null>(null);
  let keys = $state<string[]>([]);
  let regions = $state<Region[]>([]);
  let languages = $state<string[]>(['en', 'ru']);
  let form = $state<Form | null>(null);
  let removing = $state(false);
  let failure = $state('');
  let busy = $state(false);
  const editable = $derived(account.may('settings.manage'));

  async function load() {
    try {
      const [own, list, found, system] = await Promise.all([
        call(api.GET('/api/v1/tariffs/{tariff_id}', { params: { path: { tariff_id: id } } })),
        call(api.GET('/api/v1/tariffs')),
        call(api.GET('/api/v1/regions', { params: { query: { limit: 100 } } })).catch(() => ({ items: [] })),
        call(api.GET('/api/v1/system/languages')).catch(() => null)]);
      tariff = own;
      keys = list.keys;
      regions = (found as { items: Region[] }).items;
      if (system) languages = system.languages;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  function edit() {
    if (!tariff) return;
    const policy = tariff.access_policy;
    form = { titles: draftOf(tariff.titles, languages, i18n.language),
      descriptions: draftOf(tariff.descriptions, languages, tariff.titles?.source ?? i18n.language),
      term: tariff.duration_days ? String(tariff.duration_days) : '',
      mode: keepsNodes(policy) ? 'keep' : everyNode(policy) ? 'all' : 'regions', regions: chosenRegions(policy) };
    failure = '';
  }

  async function save() {
    if (!form || !tariff) return;
    failure = '';
    const titles = inputOf(form.titles, languages);
    if (!titles) {
      failure = t('vpnadmin.title_needed');
      return;
    }
    const descriptions = empty(form.descriptions) ? null : inputOf(form.descriptions, languages);
    if (!empty(form.descriptions) && !descriptions) {
      failure = t('vpnadmin.description_needed');
      return;
    }
    if (form.mode === 'regions' && !form.regions.length) {
      failure = t('vpnadmin.regions_needed');
      return;
    }
    busy = true;
    try {
      tariff = await call(api.PATCH('/api/v1/tariffs/{tariff_id}', { params: { path: { tariff_id: id } },
        headers: { 'If-Match': `"${tariff.revision}"` }, body: { titles, descriptions,
          duration_days: form.term ? Number(form.term) : null, access_policy: accessOf(form.mode, form.regions) } }));
      form = null;
      toast(t('vpnadmin.tariff_saved'));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (!tariff) return;
    if (!removing) {
      removing = true;
      return;
    }
    busy = true;
    failure = '';
    try {
      await call(api.DELETE('/api/v1/tariffs/{tariff_id}', { params: { path: { tariff_id: id } },
        headers: { 'If-Match': `"${tariff.revision}"` } }));
      toast(t('vpnadmin.tariff_deleted'));
      await goto('/admin/services/vpn?section=tariffs');
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      removing = false;
    } finally {
      busy = false;
    }
  }

  const nodes = (policy: unknown) => {
    const label = accessLabel(policy, regions);
    return 'key' in label ? t('vpnadmin.' + label.key) : label.names;
  };
  const toggle = (region: string) => {
    form!.regions = form!.regions.includes(region) ? form!.regions.filter((item) => item !== region) : [...form!.regions, region];
  };
</script>

<nav class="crumbs"><a href="/admin/services">{t('admin.services')}</a> / <a href="/admin/services/vpn?section=tariffs">VPN</a></nav>
{#if failure && !tariff}<p class="error">{failure}</p>
{:else if tariff}
  <div class="page-head"><h1>{tariff.title}</h1></div>
  <div class="columns appear">
    <div class="main">
      <section class="block">
        <div class="block-head"><h2>{t('vpnadmin.tariff')}</h2>
          {#if editable}
            <span class="row">
              {#if form}
                <button class="primary" form="tariff" disabled={busy}>{t('people.save')}</button>
                <button type="button" onclick={() => { form = null; failure = ''; }}>{t('common.cancel')}</button>
              {:else}
                <button onclick={edit}>{t('people.change')}</button>
              {/if}
            </span>
          {/if}
        </div>
        {#if failure}<p class="error">{failure}</p>{/if}
        <!-- One table: editing turns each value into its input in place. -->
        <form id="tariff" onsubmit={(event) => { event.preventDefault(); save(); }}>
          <dl class="facts" class:editing={form}>
            <div><dt>{t('vpnadmin.tariff_title')}</dt><dd class:wide={form}>
              {#if form}<Localized bind:draft={form.titles} {languages} label={t('vpnadmin.tariff_title')} max={64} named={false} />
              {:else}{tariff.title}{/if}</dd></div>
            <div><dt>{t('vpnadmin.description')}</dt><dd class:wide={form}>
              {#if form}<Localized bind:draft={form.descriptions} {languages} label={t('vpnadmin.description')} max={1000} multiline
                named={false} />
              {:else if tariff.description}<span class="text">{tariff.description}</span>{:else}<span class="muted">—</span>{/if}</dd></div>
            <div><dt>{t('vpnadmin.term')}</dt><dd>
              {#if form}
                <select bind:value={form.term} aria-label={t('vpnadmin.term')}>
                  <option value="">{t('vpnadmin.no_term')}</option>
                  {#each TERMS as days (days)}<option value={String(days)}>{t('vpnadmin.days', { count: days })}</option>{/each}
                </select>
              {:else}{tariff.duration_days ? t('vpnadmin.days', { count: tariff.duration_days }) : t('vpnadmin.no_term')}{/if}</dd></div>
            <div><dt>{t('vpnadmin.nodes')}</dt><dd>
              {#if form}
                <select bind:value={form.mode} aria-label={t('vpnadmin.nodes')}>
                  {#each ['keep', 'all', 'regions'] as mode (mode)}<option value={mode}>{t('vpnadmin.nodes_' + mode)}</option>{/each}
                </select>
              {:else}{nodes(tariff.access_policy)}{/if}</dd>
              {#if form?.mode === 'regions'}
                <div class="edit">
                  {#each regions as region (region.id)}
                    <label class="check"><input type="checkbox" checked={form.regions.includes(region.id)}
                      onchange={() => toggle(region.id)} /> {region.title}</label>
                  {:else}<span class="muted small">{t('vpnadmin.no_regions')}</span>{/each}
                </div>
              {/if}
            </div>
            <div><dt>{t('vpnadmin.profiles')}</dt><dd>{tariff.profiles}</dd></div>
          </dl>
        </form>
        <p class="muted small">{t('vpnadmin.tariff_hint')}</p>
      </section>

      {#key tariff.revision}
        <Policies subject={{ kind: 'tariff', id: tariff.id, allowed: keys }} onchange={load} />
      {/key}

      {#if editable}
        <section class="block">
          <h2>{t('vpnadmin.delete_title')}</h2>
          {#if tariff.profiles}
            <p class="muted">{t('vpnadmin.delete_in_use', { count: tariff.profiles })}</p>
          {:else}
            <p class="muted">{t('vpnadmin.delete_hint')}</p>
            <div class="row">
              <button class="danger" class:armed={removing} disabled={busy} onclick={remove}>{t('vpnadmin.delete')}</button>
              {#if removing}<Confirm text={t('vpnadmin.delete_confirm', { title: tariff.title })} action={t('vpnadmin.delete')}
                cancel={() => (removing = false)} />{/if}
            </div>
          {/if}
        </section>
      {/if}
    </div>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  dl.editing > div {
    flex-wrap: wrap;
  }
  /* A text in every language opens under its name, the row's whole width. */
  dl.editing dd.wide {
    flex-basis: 100%;
    text-align: left;
  }
  dl.editing dd select {
    width: auto;
  }
  .edit {
    flex-basis: 100%;
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.4rem 1.25rem;
  }
  .text {
    white-space: pre-line;
  }
</style>
