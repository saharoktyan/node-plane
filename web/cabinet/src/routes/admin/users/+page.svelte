<!-- Accounts: with access, without it, or all; a person is added by their
     Telegram or by name, and opens in their card. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName, provisionBody, telegramOf } from '$lib/admin/people';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';

  type Access = 'with' | 'without' | 'all';
  type Account = Schemas['AdminAccountOutput'];
  let access = $state<Access>('with');
  let items = $state<Account[]>([]);
  let cursor = $state<string | null>(null);
  let loading = $state(true);
  let failure = $state('');
  let adding = $state(false);
  let identity = $state('');
  let busy = $state(false);

  async function load(more = false) {
    loading = true;
    failure = '';
    try {
      const page = await call(api.GET('/api/v1/accounts', { params: { query: {
        access, limit: 100, cursor: more ? (cursor ?? undefined) : undefined } } }));
      items = more ? [...items, ...page.items] : page.items;
      cursor = page.next_cursor;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void access;
    load();
  });

  async function add(event: SubmitEvent) {
    event.preventDefault();
    if (!identity.trim()) return;
    busy = true;
    failure = '';
    try {
      // The account comes with its VPN profile, as in the bot and Workstation.
      const created = (await call(api.POST('/api/v1/accounts', { body: provisionBody(identity),
        headers: { 'Idempotency-Key': key() } }))) as { account: { id: string } };
      await goto('/admin/users/' + created.account.id);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

<div class="page-head">
  <h1>{t('admin.users')}</h1>
  {#if account.may('accounts.manage') && !adding}
    <div class="actions"><button class="primary" onclick={() => (adding = true)}>{t('people.add')}</button></div>
  {/if}
</div>
{#if adding}
  <form class="row add" onsubmit={add}>
    <!-- svelte-ignore a11y_autofocus -->
    <input class="identity" maxlength="128" placeholder={t('people.add_placeholder')} aria-label={t('people.add_placeholder')}
      bind:value={identity} autofocus />
    <button class="primary" disabled={busy || !identity.trim()}>{t('people.add_confirm')}</button>
    <button type="button" onclick={() => { adding = false; identity = ''; }}>{t('common.cancel')}</button>
    <p class="muted hint">{t('people.add_hint')}</p>
  </form>
{/if}
<div class="tabs" role="tablist" aria-label={t('admin.users')}>
  {#each ['with', 'without', 'all'] as const as value (value)}
    <button role="tab" aria-selected={access === value} onclick={() => (access = value)}>{t('admin.access_' + value)}</button>
  {/each}
</div>
{#if failure}<p class="error">{failure}</p>{/if}
<div class="table"><table>
  <thead><tr><th>{t('admin.name')}</th><th>{t('auth.login')}</th><th>Telegram</th><th>{t('admin.roles')}</th>
    <th>{t('admin.state')}</th></tr></thead>
  <tbody>
    {#each items as item (item.id)}
      <tr class="link" onclick={() => goto('/admin/users/' + item.id)}>
        <td class="name"><a href={'/admin/users/' + item.id}>{personName(item)}</a></td>
        <td>{item.login ?? '—'}</td>
        <td>{telegramOf(item)}</td>
        <td>{(item.roles ?? []).map((role) => t('admin.role_' + role)).join(', ') || t('admin.role_member')}</td>
        <td>{#if item.status === 'disabled'}<span class="badge danger">{t('people.blocked')}</span>
          {:else if item.deleted}<span class="badge warn">{t('people.deleting')}</span>{:else}—{/if}</td>
      </tr>
    {:else}
      {#if !loading}<tr><td colspan="5" class="muted">{t('admin.no_accounts')}</td></tr>{/if}
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
  .identity {
    flex: 1 1 16rem;
    max-width: 24rem;
  }
  .hint {
    flex-basis: 100%;
    margin: 0;
    font-size: 13px;
  }
</style>
