<!-- VPN profiles: found by name, opened in their card; a new one is made for
     an account or on its own and gets its access in the card. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, key } from '$lib/ui/format';

  type Profile = Schemas['ProfileOutput'];
  let search = $state('');
  let items = $state<Profile[]>([]);
  let cursor = $state<string | null>(null);
  let loading = $state(true);
  let failure = $state('');
  let creating = $state(false);
  let name = $state('');
  let busy = $state(false);

  async function load(more = false) {
    loading = true;
    failure = '';
    try {
      const query = search.trim();
      const page = await call(api.GET('/api/v1/profiles', { params: { query: { limit: 100,
        search: query || undefined, cursor: more ? (cursor ?? undefined) : undefined } } }));
      items = more ? [...items, ...page.items] : page.items;
      cursor = page.next_cursor;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      loading = false;
    }
  }
  load();

  let timer: ReturnType<typeof setTimeout> | undefined;
  function searched() {
    clearTimeout(timer);
    timer = setTimeout(() => load(), 300);
  }

  async function create(event: SubmitEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    busy = true;
    failure = '';
    try {
      const made = await call(api.POST('/api/v1/profiles', { params: { header: { 'Idempotency-Key': key() } },
        body: { display_name: name.trim(), grants: [] } }));
      await goto('/admin/profiles/' + made.profile.id);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const expired = (item: Profile) => item.expires_at != null && new Date(item.expires_at) < new Date();
</script>

<div class="page-head">
  <h1>{t('profiles.title')}</h1>
  {#if account.may('profiles.manage') && !creating}
    <div class="actions"><button class="primary" onclick={() => (creating = true)}>{t('profiles.new')}</button></div>
  {/if}
</div>
{#if creating}
  <form class="row add" onsubmit={create}>
    <!-- svelte-ignore a11y_autofocus -->
    <input class="name" maxlength="128" placeholder={t('profiles.name')} aria-label={t('profiles.name')} bind:value={name} autofocus />
    <button class="primary" disabled={busy || !name.trim()}>{t('profiles.create')}</button>
    <button type="button" onclick={() => { creating = false; name = ''; }}>{t('common.cancel')}</button>
    <p class="muted hint">{t('profiles.new_hint')}</p>
  </form>
{/if}
<div class="row search">
  <input type="search" placeholder={t('profiles.search')} aria-label={t('profiles.search')} bind:value={search} oninput={searched} />
</div>
{#if failure}<p class="error">{failure}</p>{/if}
<div class="table"><table>
  <thead><tr><th>{t('profiles.name')}</th><th>{t('profiles.expires')}</th><th>{t('admin.state')}</th></tr></thead>
  <tbody>
    {#each items as item (item.id)}
      <tr class="link" onclick={() => goto('/admin/profiles/' + item.id)}>
        <td class="name"><a href={'/admin/profiles/' + item.id}>{item.display_name}</a></td>
        <td>{item.expires_at ? date(item.expires_at) : t('profiles.forever')}</td>
        <td>
          {#if item.deleting}<span class="badge warn">{t('profiles.deleting')}</span>
          {:else if item.frozen}<span class="badge warn">{t('profiles.frozen')}</span>
          {:else if expired(item)}<span class="badge danger">{t('profiles.expired')}</span>
          {:else}<span class="badge ok">{t('profiles.active')}</span>{/if}
        </td>
      </tr>
    {:else}
      {#if !loading}<tr><td colspan="3" class="muted">{t('profiles.none')}</td></tr>{/if}
    {/each}
  </tbody>
</table></div>
{#if loading}<p class="muted">{t('common.loading')}</p>
{:else if cursor}<p><button onclick={() => load(true)}>{t('people.more')}</button></p>{/if}

<style>
  .name {
    font-weight: 600;
  }
  .name a {
    color: inherit;
  }
  tr.link {
    cursor: pointer;
  }
  .add {
    flex-wrap: wrap;
    padding: 0 0 1rem;
  }
  .add .name {
    flex: 1 1 16rem;
    max-width: 24rem;
  }
  .hint {
    flex-basis: 100%;
    margin: 0;
    font-size: 13px;
  }
  .search {
    margin-bottom: 1rem;
    max-width: 24rem;
  }
</style>
