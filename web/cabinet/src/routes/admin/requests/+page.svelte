<!-- Access requests waiting for a decision. Approving one creates the
     account with its VPN profile; rejecting it leaves no account. Below,
     whether new people may ask at all. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName } from '$lib/admin/people';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime, key } from '$lib/ui/format';

  type Request = Schemas['AccessRequestOutput'];
  let items = $state<Request[] | null>(null);
  let policy = $state<Schemas['AccessRequestPolicyOutput'] | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let confirming = $state<{ id: string; decision: 'approve' | 'reject'; key: string } | null>(null);

  async function load() {
    try {
      items = (await call(api.GET('/api/v1/access-requests', { params: { query: { limit: 100 } } }))).items;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
    if (account.may('settings.manage'))
      policy = await call(api.GET('/api/v1/system/access-requests')).catch(() => null);
  }
  load();

  async function decide() {
    if (!confirming) return;
    const { id, decision, key: commandKey } = confirming;
    busy = true;
    failure = notice = '';
    try {
      await call(api.POST('/api/v1/access-requests/{request_id}/decision', { params: { path: { request_id: id },
        header: { 'Idempotency-Key': commandKey } }, body: { decision } }));
      notice = t(decision === 'approve' ? 'people.request_approved' : 'people.request_rejected');
      confirming = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function setPolicy(changes: Schemas['AccessRequestPolicyInput']) {
    busy = true;
    failure = '';
    try {
      policy = await call(api.PATCH('/api/v1/system/access-requests', { body: changes }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

<div class="page-head"><h1>{t('people.requests')}</h1></div>
{#if notice}<p class="muted">{notice}</p>{/if}
{#if failure}<p class="error">{failure}</p>{/if}
{#if items === null}
  {#if !failure}<p class="muted">{t('common.loading')}</p>{/if}
{:else}
  <section class="block">
    {#if !items.length}<p class="muted">{t('people.no_requests')}</p>{/if}
    <div class="rows">
      {#each items as item (item.id)}
        <div>
          <span class="grow"><strong>{personName(item)}</strong>
            <span class="muted small">{item.username ? '@' + item.username + ' · ' : ''}{item.telegram_user_id ?? ''}
              · {dateTime(item.created_at)}</span></span>
          {#if item.status === 'pending'}
            <button class="primary" onclick={() => (confirming = { id: item.id, decision: 'approve', key: key() })}>
              {t('people.approve')}</button>
            <button onclick={() => (confirming = { id: item.id, decision: 'reject', key: key() })}>{t('people.reject')}</button>
          {:else}
            <span class="badge">{t('people.request_' + item.status + (item.status === 'pending' ? '' : '_state'))}</span>
          {/if}
        </div>
        {#if confirming?.id === item.id}
          <Confirm text={t('people.' + confirming.decision + '_confirm', { name: personName(item) })}
            action={t('people.' + confirming.decision)} danger={confirming.decision === 'reject'} {busy}
            confirm={decide} cancel={() => (confirming = null)} />
        {/if}
      {/each}
    </div>
  </section>
{/if}

{#if policy}
  <section class="block">
    <h2>{t('people.intake')}</h2>
    <label class="check"><input type="checkbox" checked={policy.enabled} disabled={busy}
      onchange={(event) => setPolicy({ enabled: event.currentTarget.checked })} />
      {t('people.intake_open')}</label>
    <p class="muted small">{t('people.intake_hint')}</p>
    <label class="check"><input type="checkbox" checked={policy.notify_requests ?? false} disabled={busy}
      onchange={(event) => setPolicy({ notify_requests: event.currentTarget.checked })} />
      {t('people.intake_notify')}</label>
  </section>
{/if}

<style>
  .rows > div {
    padding: 0.4rem 0;
  }
  .small {
    font-size: 13px;
    margin-left: 0.5rem;
  }
</style>
