<!-- A profile's traffic quota: the current period's use against the limit,
     whether it is stopped or slowed, the past periods, a reset now and,
     for a monthly reset from a chosen day, that day. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';

  let { profileId, revision, onchange }: { profileId: string; revision: number; onchange: () => Promise<void> } = $props();

  let quota = $state<Schemas['QuotaOutput'] | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let resetting = $state<string | null>(null);
  let day = $state<string | null>(null);

  const path = () => ({ path: { profile_id: profileId } });

  async function load() {
    failure = '';
    try {
      quota = await call(api.GET('/api/v1/profiles/{profile_id}/quota', { params: path() }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  // The limits and the profile's revision move the quota too.
  $effect(() => {
    void revision;
    load();
  });

  async function run(work: () => Promise<Schemas['QuotaOutput']>, done: string) {
    busy = true;
    failure = notice = '';
    try {
      quota = await work();
      notice = t(done);
      return true;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      return false;
    } finally {
      busy = false;
    }
  }

  async function reset() {
    if (!resetting) return;
    const commandKey = resetting;
    if (await run(() => call(api.POST('/api/v1/profiles/{profile_id}/quota/reset',
      { params: { ...path(), header: { 'Idempotency-Key': commandKey } } })), 'quota.reset_done')) {
      resetting = null;
      await onchange();
    }
  }

  async function move(event: SubmitEvent) {
    event.preventDefault();
    if (!day || !quota) return;
    // A plain date: its midnight in the quota's time zone.
    if (await run(() => call(api.PATCH('/api/v1/profiles/{profile_id}/quota', { params: path(), body: { anchor_at: day! },
      headers: { 'If-Match': `"${quota!.anchor_revision}"` } })), 'quota.anchor_saved')) day = null;
  }

  const share = $derived(quota?.limit_bytes && quota.period ? Math.min(1, quota.period.used_bytes / quota.limit_bytes) : 0);
  const reason = (value: string | null) => (value ? t('quota.closed_' + value) : '');
</script>

<section class="block">
  <h2>{t('quota.title')}</h2>
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}
  {#if quota}
    {#if quota.limit_bytes == null}
      <p class="muted">{t('quota.none')}</p>
    {:else}
      <p>
        <strong>{bytes(quota.period?.used_bytes ?? 0)}</strong> {t('quota.of', { limit: bytes(quota.limit_bytes) })}
        {#if quota.period?.blocked}<span class="badge danger">{t('quota.stopped')}</span>
        {:else if quota.throttled}<span class="badge warn">{t('quota.slowed', { speed: quota.throttled / 1000 })}</span>{/if}
      </p>
      <div class="meter" class:full={share >= 1} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(share * 100)}
        aria-label={t('quota.title')}><span style:width={share * 100 + '%'}></span></div>
      <p class="muted small">
        {#if quota.period}{t('quota.period', { start: date(quota.period.start), end: quota.period.end ? date(quota.period.end) : '—' })}
        {:else}{t('quota.no_period')}{/if}
        · {t('policies.quota_reset_' + quota.reset)}{#if quota.reset === 'interval'} ({t('policies.days', { n: quota.interval_days })}){/if}
        · {t('policies.quota_on_exhausted_' + quota.on_exhausted)}
      </p>
    {/if}
    {#if quota.reset === 'monthly_anchor' && quota.anchor_day}
      {#if day === null}
        <p class="small">{t('quota.anchor', { day: date(quota.anchor_day) })}
          {#if account.may('profiles.manage')}<button class="bare link" onclick={() => (day = quota?.anchor_day ?? '')}>{t('people.change')}</button>{/if}</p>
      {:else}
        <form class="row" onsubmit={move}>
          <input type="date" bind:value={day} aria-label={t('quota.anchor_label')} />
          <button class="primary" disabled={busy || !day}>{t('people.save')}</button>
          <button type="button" onclick={() => (day = null)}>{t('common.cancel')}</button>
        </form>
        <p class="muted small">{t('quota.anchor_hint', { zone: quota.timezone })}</p>
      {/if}
    {/if}
    {#if quota.period && account.may('profiles.manage')}
      {#if resetting}
        <Confirm text={t('quota.reset_confirm')} action={t('quota.reset')} {busy} confirm={reset} cancel={() => (resetting = null)} />
      {:else}
        <p><button onclick={() => (resetting = key())}>{t('quota.reset')}</button></p>
      {/if}
    {/if}
    {#if quota.history.length}
      <h3>{t('quota.history')}</h3>
      <div class="table"><table>
        <tbody>
          {#each quota.history as period (period.id)}
            <tr><td>{date(period.start)} — {date(period.closed_at ?? period.end)}</td><td>{bytes(period.used_bytes)}</td>
              <td class="muted">{[reason(period.close_reason), period.exhausted_at ? t('quota.was_used_up') : ''].filter(Boolean).join(' · ')}</td></tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  {:else if !failure}<p class="muted">{t('common.loading')}</p>{/if}
</section>

<style>
  .small {
    font-size: 13px;
  }
  .link {
    margin-left: 0.5rem;
    text-decoration: underline;
  }
  h3 {
    margin: 1.25rem 0 0.25rem;
    font-size: 1rem;
  }
  .badge {
    margin-left: 0.5rem;
  }
  input[type='date'] {
    width: auto;
  }
  .meter {
    height: 6px;
    background: var(--line);
    border-radius: 3px;
    overflow: hidden;
    margin: 0.25rem 0 0.5rem;
  }
  .meter span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .meter.full span {
    background: var(--danger, var(--accent));
  }
</style>
