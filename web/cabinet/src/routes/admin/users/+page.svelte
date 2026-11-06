<!-- Users: accounts with access, without it, or all. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';

  type Access = 'with' | 'without' | 'all';
  let access = $state<Access>('with');
  const load = $derived(call(api.GET('/api/v1/accounts', { params: { query: { access, limit: 100 } } })));

  function name(item: { login?: string | null; display_name?: string | null; username?: string | null; id: string }) {
    return item.display_name || item.login || (item.username ? '@' + item.username : item.id.slice(0, 8));
  }
</script>

<div class="page-head">
  <h1>{t('admin.users')}</h1>
  <div class="actions">
    {#each ['with', 'without', 'all'] as const as value (value)}
      <button class:primary={access === value} onclick={() => (access = value)}>{t('admin.access_' + value)}</button>
    {/each}
  </div>
</div>
<Async {load}>
  {#snippet children(page)}
    <table>
      <thead><tr><th>{t('admin.name')}</th><th>{t('auth.login')}</th><th>Telegram</th><th>{t('admin.roles')}</th></tr></thead>
      <tbody>
        {#each page.items as item (item.id)}
          <tr>
            <td>{name(item)}</td>
            <td>{item.login ?? '—'}</td>
            <td>{item.username ? '@' + item.username : (item.telegram_user_id ?? '—')}</td>
            <td>{(item.roles ?? []).map((role) => t('admin.role_' + role)).join(', ') || t('admin.role_member')}</td>
          </tr>
        {:else}
          <tr><td colspan="4" class="muted">{t('admin.no_accounts')}</td></tr>
        {/each}
      </tbody>
    </table>
  {/snippet}
</Async>

