<!-- Requests waiting for a decision. Approving access creates the account
     with its VPN profile; rejecting it leaves no account. A renewal extends
     the profile by its tariff's days, or by the days staff name. Below,
     whether new people may ask at all. -->
<script lang="ts">
  import { toast } from '$lib/ui/toast.svelte';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName } from '$lib/admin/people';
  import Localized from '$lib/admin/Localized.svelte';
  import { draftOf, inputOf, shownText, type Draft } from '$lib/admin/localized';
  import { i18n, t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, dateTime, key } from '$lib/ui/format';

  type Request = Schemas['AccessRequestOutput'];
  type Renewal = { id: string; profile_id: string; account_id: string | null; created_at: string;
    display_name: string; profile_expires_at: string | null; profile_revision: number;
    tariff: { title: string; duration_days: number | null } | null };
  let tab = $state<'access' | 'renewal'>(account.may('access_requests.manage') ? 'access' : 'renewal');
  let items = $state<Request[] | null>(null);
  let renewals = $state<Renewal[] | null>(null);
  let days = $state<Record<string, number>>({});
  let policy = $state<Schemas['AccessRequestPolicyOutput'] | null>(null);
  let failure = $state('');
  let busy = $state(false);
  // The bot's message to newcomers: a welcome while requests are open, else the stub.
  let message = $state<Draft | null>(null);
  let languages = $state<string[]>(['en', 'ru']);
  let confirming = $state<{ id: string; decision: 'approve' | 'reject'; key: string } | null>(null);

  async function load() {
    try {
      if (account.may('access_requests.manage'))
        items = (await call(api.GET('/api/v1/access-requests', { params: { query: { limit: 100 } } }))).items;
      if (account.may('profiles.manage')) renewals = await allRenewals();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
    if (account.may('settings.manage')) {
      policy = await call(api.GET('/api/v1/system/access-requests')).catch(() => null);
      const system = await call(api.GET('/api/v1/system/languages')).catch(() => null);
      if (system) languages = system.languages;
    }
  }

  const messageField = $derived(policy?.enabled ? 'welcome_messages' : 'gate_messages');
  async function saveMessage() {
    if (!message) return;
    const texts = inputOf(message, languages);
    if (!texts) {
      failure = t('people.message_needed');
      return;
    }
    await setPolicy({ [messageField]: texts });
    if (!failure) message = null;
  }
  load();

  async function decide() {
    if (!confirming) return;
    const { id, decision, key: commandKey } = confirming;
    busy = true;
    failure = '';
    try {
      await call(api.POST('/api/v1/access-requests/{request_id}/decision', { params: { path: { request_id: id },
        header: { 'Idempotency-Key': commandKey } }, body: { decision } }));
      toast(t(decision === 'approve' ? 'people.request_approved' : 'people.request_rejected'));
      confirming = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function allRenewals() {
    const result: Renewal[] = [];
    for (let offset = 0; ; ) {
      const page = (await call(api.GET('/api/v1/renewal-requests', { params: { query: { offset } } }))) as unknown as
        { total: number; items: Renewal[] };
      result.push(...page.items);
      offset += page.items.length;
      if (!page.items.length || offset >= page.total) return result;
    }
  }

  async function renew(item: Renewal, decision: 'approve' | 'reject') {
    busy = true;
    failure = '';
    try {
      const path = { path: { request_id: item.id } };
      if (decision === 'approve') {
        await call(api.POST('/api/v1/renewal-requests/{request_id}/approve', { params: { ...path,
          header: { 'Idempotency-Key': key(), 'If-Match': `"${item.profile_revision}"` } },
          body: { days: item.tariff?.duration_days ? null : (days[item.id] ?? 30) } }));
      } else {
        await call(api.POST('/api/v1/renewal-requests/{request_id}/reject', { params: path }));
      }
      toast(t(decision === 'approve' ? 'profiles.renewal_approved' : 'profiles.renewal_rejected'));
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
{#if failure}<p class="error">{failure}</p>{/if}
{#if items !== null && renewals !== null}
  <div class="tabs" role="tablist" aria-label={t('people.requests')}>
    {#each ['access', 'renewal'] as const as name (name)}
      <button role="tab" aria-selected={tab === name} class:active={tab === name} onclick={() => (tab = name)}>
        {t('people.requests_' + name)} <span class="muted">{name === 'access' ? items.filter((i) => i.status === 'pending').length
          : renewals.length}</span></button>
    {/each}
  </div>
{/if}
{#if tab === 'renewal'}
  {#if renewals !== null}
    <section class="block">
      {#if !renewals.length}<p class="muted">{t('people.no_renewals')}</p>{/if}
      <div class="rows">
        {#each renewals as item (item.id)}
          <div>
            <span class="grow"><strong>{#if item.account_id}<a href={'/admin/users/' + item.account_id + '/vpn'}>{item.display_name}</a>
              {:else}{item.display_name}{/if}</strong>
              <span class="muted small">{t('people.renewal_line', { until: item.profile_expires_at ? date(item.profile_expires_at)
                : t('profiles.forever'), tariff: item.tariff?.title ?? t('profiles.no_tariff') })} · {dateTime(item.created_at)}</span></span>
            {#if item.tariff?.duration_days}
              <span class="muted small">{t('profiles.renewal_by', { days: item.tariff.duration_days })}</span>
            {:else}
              <input class="days" type="number" min="1" max="3650" value={days[item.id] ?? 30}
                oninput={(event) => (days[item.id] = Number(event.currentTarget.value))} aria-label={t('profiles.renewal_days')} />
              <span class="muted small">{t('policies.unit_days')}</span>
            {/if}
            <button class="primary" disabled={busy} onclick={() => renew(item, 'approve')}>{t('people.approve')}</button>
            <button disabled={busy} onclick={() => renew(item, 'reject')}>{t('people.reject')}</button>
          </div>
        {/each}
      </div>
    </section>
  {/if}
{:else if items !== null}
  <section class="block">
    {#if !items.length}<p class="muted">{t('people.no_requests')}</p>{/if}
    <div class="rows">
      {#each items as item (item.id)}
        <div>
          <span class="grow"><strong>{personName(item)}</strong>
            <span class="muted small">{item.username ? '@' + item.username + ' · ' : ''}{item.telegram_user_id ?? ''}
              · {dateTime(item.created_at)}</span></span>
          {#if item.status === 'pending'}
            {#each ['approve', 'reject'] as const as decision (decision)}
              {@const armed = confirming?.id === item.id && confirming.decision === decision}
              <button class:primary={decision === 'approve'} class:armed class:danger={armed && decision === 'reject'} disabled={busy}
                onclick={() => (armed ? decide() : (confirming = { id: item.id, decision, key: key() }))}>{t('people.' + decision)}</button>
            {/each}
          {:else}
            <span class="badge">{t('people.request_' + item.status + (item.status === 'pending' ? '' : '_state'))}</span>
          {/if}
        </div>
        {#if confirming?.id === item.id}
          <Confirm text={t('people.' + confirming.decision + '_confirm', { name: personName(item) })}
            action={t('people.' + confirming.decision)}
            cancel={() => (confirming = null)} />
        {/if}
      {/each}
    </div>
  </section>
{/if}

{#if policy && tab === 'access'}
  <section class="block">
    <h2>{t('people.intake')}</h2>
    <label class="check"><input type="checkbox" checked={policy.enabled} disabled={busy}
      onchange={(event) => { message = null; setPolicy({ enabled: event.currentTarget.checked }); }} />
      {t('people.intake_open')}</label>
    <p class="muted small">{t('people.intake_hint')}</p>
    <label class="check"><input type="checkbox" checked={policy.notify_requests ?? false} disabled={busy}
      onchange={(event) => setPolicy({ notify_requests: event.currentTarget.checked })} />
      {t('people.intake_notify')}</label>
    {#if policy.telegram}
      {@const stored = policy[messageField]}
      <div class="block-head message"><h3>{t(policy.enabled ? 'people.welcome_message' : 'people.gate_message')}</h3>
        <span class="row">
          {#if message}
            <button class="primary" disabled={busy} onclick={saveMessage}>{t('people.save')}</button>
            <button onclick={() => (message = null)}>{t('common.cancel')}</button>
          {:else}
            <button onclick={() => (message = draftOf(stored, languages, i18n.language))}>{t('people.change')}</button>
          {/if}
        </span>
      </div>
      <p class="muted small">{t(policy.enabled ? 'people.welcome_hint' : 'people.gate_hint')}</p>
      {#if message}
        <Localized bind:draft={message} {languages} label={t(policy.enabled ? 'people.welcome_message' : 'people.gate_message')}
          max={500} multiline named={false} />
      {:else}
        <p class="text">{shownText(stored, i18n.language) || (policy.enabled ? policy.welcome_message : policy.gate_message)}</p>
      {/if}
    {/if}
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
  .message {
    margin-top: 1.5rem;
  }
  .message h3 {
    margin: 0;
  }
  .text {
    white-space: pre-line;
  }
  input.days {
    width: 4.5rem;
    flex: none;
  }
</style>
