<!-- Linking a Workstation: the code it shows is confirmed here by a staff
     member, and the Workstation then acts with this account's roles. -->
<script lang="ts">
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';

  let code = $state(page.url.searchParams.get('code') ?? '');
  let preview = $state<Schemas['PairingPreview'] | null>(null);
  let result = $state('');
  let error = $state('');

  async function attempt(work: () => Promise<void>) {
    error = '';
    try {
      await work();
    } catch (failure) {
      error = failure instanceof ApiError ? errorText(failure) : t('common.error');
    }
  }

  const look = (event?: SubmitEvent) => {
    event?.preventDefault();
    return attempt(async () => {
      preview = await call(api.GET('/api/v1/workstation-pairings/code/{code}', { params: { path: { code } } }));
    });
  };

  const decide = (approve: boolean) =>
    attempt(async () => {
      const path = approve
        ? '/api/v1/workstation-pairings/code/{code}/approve'
        : '/api/v1/workstation-pairings/code/{code}/reject';
      const decision = await call(api.POST(path, { params: { path: { code } } }));
      result = t(decision.status === 'approved' ? 'pair.approved' : 'pair.rejected', { name: decision.name });
      preview = null;
    });

  // A link from Workstation brings the code along.
  const given = page.url.searchParams.get('code');
  if (given && account.staff) look();
</script>

<div class="page-head"><h1>{t('pair.title')}</h1></div>

{#if !account.staff}
  <p class="muted">{t('pair.staff_only')}</p>
{:else if result}
  <p>{result}</p>
{:else if preview}
  <p>{t('pair.confirm', { name: preview.name })}</p>
  <p class="muted">{t('pair.details', { code: preview.code, source: preview.source, date: dateTime(preview.created_at) })}</p>
  <p class="muted">{t('pair.warning')}</p>
  <div class="row">
    <button class="primary" onclick={() => decide(true)}>{t('pair.approve')}</button>
    <button onclick={() => decide(false)}>{t('pair.reject')}</button>
  </div>
{:else}
  <form onsubmit={look}>
    <label>{t('pair.code')}<input bind:value={code} autocomplete="off" required /></label>
    <button class="primary">{t('pair.continue')}</button>
  </form>
{/if}
{#if error}<p class="error">{error}</p>{/if}
