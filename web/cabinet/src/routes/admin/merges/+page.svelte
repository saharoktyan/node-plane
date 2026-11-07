<!-- Merge requests: a person asked to join two of their accounts. The list
     names both and whether a VPN profile has to be chosen; a request opens
     with what the merge does and its decision. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName } from '$lib/admin/people';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';

  type Side = { login: string | null; display_name: string | null; username: string | null; telegram_user_id: number | null };
  type Preview = { main: Side; other: Side; result: { vpn: 'moves' | 'choose' | null } };
  type Merge = Schemas['MergeRequestOutput'];

  let items = $state<Merge[] | null>(null);
  let failure = $state('');

  call(api.GET('/api/v1/merge-requests'))
    .then((page) => (items = [...page.items].sort((a, b) => Number(b.status === 'pending') - Number(a.status === 'pending')
      || b.created_at.localeCompare(a.created_at))))
    .catch((error) => (failure = error instanceof ApiError ? errorText(error) : t('common.error')));

  const preview = (item: Merge) => item.preview as unknown as Preview | null;
  const open = (item: Merge) => goto('/admin/merges/' + item.id);
</script>

<div class="page-head"><h1>{t('people.merges')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}
{#if items === null}
  {#if !failure}<p class="muted">{t('common.loading')}</p>{/if}
{:else if !items.length}
  <p class="muted">{t('people.no_merges')}</p>
{:else}
  <div class="table"><table>
    <thead><tr><th>{t('people.merge_main')}</th><th>{t('people.merge_other')}</th><th>{t('people.merge_asked')}</th>
      <th>{t('admin.state')}</th></tr></thead>
    <tbody>
      {#each items as item (item.id)}
        {@const seen = preview(item)}
        <tr class="link" onclick={() => open(item)}>
          <td class="name"><a href={'/admin/merges/' + item.id}>{seen ? personName(seen.main) : item.main_account_id.slice(0, 8)}</a></td>
          <td>{seen ? personName(seen.other) : item.other_account_id.slice(0, 8)}
            {#if item.status === 'pending' && seen?.result.vpn === 'choose'}<span class="muted small">· {t('people.merge_choose_vpn')}</span>{/if}</td>
          <td>{dateTime(item.created_at)}</td>
          <td><span class="badge" class:warn={item.status === 'pending'}>{t('people.merge_' + item.status)}</span></td>
        </tr>
      {/each}
    </tbody>
  </table></div>
{/if}

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
  .small {
    font-size: 13px;
  }
</style>
