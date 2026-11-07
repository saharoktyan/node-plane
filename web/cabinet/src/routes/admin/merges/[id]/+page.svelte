<!-- One merge request: both accounts and what joining them does; when both
     have VPN, one profile is kept. The other account is deleted. -->
<script lang="ts">
  import { page } from '$app/state';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName } from '$lib/admin/people';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, dateTime } from '$lib/ui/format';

  type Side = { id: string; login: string | null; display_name: string | null; username: string | null;
    telegram_user_id: number | null; role: string | null; has_password: boolean; passkeys: number;
    vpn: { devices: number; tariff: string | null; expires_at: string | null } | null };
  type Preview = { main: Side; other: Side; result: { vpn: 'moves' | 'choose' | null; telegram: string | null;
    password: string | null; login: string | null; passkeys_removed: number; keep_main_only: boolean } };
  type Merge = Schemas['MergeRequestOutput'];

  const id = $derived(page.params.id as string);
  let item = $state<Merge | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let confirming = $state<'approve' | 'reject' | null>(null);
  let keep = $state<'main' | 'other' | null>(null);

  async function load() {
    try {
      item = await call(api.GET('/api/v1/merge-requests/{request_id}', { params: { path: { request_id: id } } }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  $effect(() => {
    void id;
    load();
  });

  const seen = $derived(item?.preview as unknown as Preview | null);

  async function decide() {
    if (!confirming || !item) return;
    busy = true;
    failure = notice = '';
    try {
      await call(api.POST('/api/v1/merge-requests/{request_id}/decision', { params: { path: { request_id: item.id } },
        body: { decision: confirming, keep_profile: confirming === 'approve' ? keep : null } }));
      notice = t(confirming === 'approve' ? 'people.merge_done' : 'people.merge_rejected_done');
      confirming = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  function vpnLine(side: Side) {
    return side.vpn ? t('people.merge_vpn', { devices: side.vpn.devices, tariff: side.vpn.tariff ?? '—',
      until: date(side.vpn.expires_at) }) : t('people.merge_no_vpn');
  }
</script>

<nav class="crumbs"><a href="/admin/merges">{t('people.merges')}</a></nav>
{#if failure && !item}<p class="error">{failure}</p>
{:else if !item}<p class="muted">{t('common.loading')}</p>
{:else}
  <div class="page-head">
    <h1>{seen ? personName(seen.main) + ' ← ' + personName(seen.other) : item.id.slice(0, 8)}</h1>
    <div class="actions"><span class="badge">{t('people.merge_' + item.status)}</span></div>
  </div>
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}
  <section class="block">
    <p class="muted small">{dateTime(item.created_at)}</p>
    {#if seen}
      <div class="sides">
        {#each [['main', seen.main], ['other', seen.other]] as const as [role, side] (role)}
          <div>
            <div class="caps">{t('people.merge_' + role)}</div>
            <strong><a href={'/admin/users/' + side.id}>{personName(side)}</a></strong>
            <div class="muted small">{side.username ? '@' + side.username : (side.telegram_user_id ?? t('people.merge_no_telegram'))}</div>
            <div class="small">{vpnLine(side)}</div>
          </div>
        {/each}
      </div>
      <ul class="small result">
        {#if seen.result.vpn === 'moves'}<li>{t('people.merge_vpn_moves')}</li>{/if}
        {#each ['telegram', 'password', 'login'] as const as part (part)}
          {#if seen.result[part]}<li>{t('people.merge_' + part + '_' + seen.result[part])}</li>{/if}
        {/each}
        {#if seen.result.passkeys_removed}<li>{t('people.merge_passkeys', { count: seen.result.passkeys_removed })}</li>{/if}
        <li>{t('people.merge_other_deleted')}</li>
      </ul>
      {#if item.status === 'pending' && seen.result.vpn === 'choose'}
        <fieldset class="keep">
          <legend>{t('people.merge_keep')}</legend>
          {#each ['main', 'other'] as const as side (side)}
            <label class="check"><input type="radio" name="keep" value={side}
              disabled={side === 'other' && seen.result.keep_main_only} bind:group={keep} />
              {t('people.merge_keep_' + side)}</label>
          {/each}
          {#if seen.result.keep_main_only}<p class="muted small">{t('people.merge_keep_main_only')}</p>{/if}
        </fieldset>
      {/if}
    {/if}
    {#if item.status === 'pending'}
      {#if confirming}
        <Confirm text={t('people.merge_' + confirming + '_confirm')} action={t('people.' + confirming)}
          danger={confirming === 'approve'} {busy} confirm={decide} cancel={() => (confirming = null)} />
      {:else}
        <div class="row">
          <button class="primary" disabled={seen?.result.vpn === 'choose' && !keep}
            onclick={() => (confirming = 'approve')}>{t('people.approve')}</button>
          <button onclick={() => (confirming = 'reject')}>{t('people.reject')}</button>
        </div>
      {/if}
    {/if}
  </section>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .sides {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: 1rem;
    margin: 0.5rem 0;
  }
  .result {
    margin: 0.5rem 0;
    padding-left: 1.2rem;
  }
  .keep {
    border: none;
    padding: 0;
    margin: 0.5rem 0;
  }
</style>
