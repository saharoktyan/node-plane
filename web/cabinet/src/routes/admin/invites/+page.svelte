<!-- Invites and open registration: the ways new people come in. An invite
     makes a new account, with VPN and a tariff or without access; its link
     is shown once. Registration lets anyone make an account with a login. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, dateTime } from '$lib/ui/format';

  type Invite = Schemas['InviteOutput'];
  let items = $state<Invite[] | null>(null);
  let tariffs = $state<Schemas['TariffOutput'][]>([]);
  let bot = $state<Schemas['TelegramBotUsername'] | null>(null);
  let registration = $state<Schemas['RegistrationSettings'] | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let creating = $state(false);
  let draft = $state({ vpn: true, tariff: '', days: 7, note: '' });
  let created = $state<Schemas['CreatedInvite'] | null>(null);
  let revoking = $state<string | null>(null);
  let botName = $state('');
  let cleanup = $state<number | null>(null);

  async function load() {
    try {
      items = (await call(api.GET('/api/v1/invites'))).items;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
    bot = await call(api.GET('/api/v1/invites/telegram-bot')).catch(() => null);
    botName = bot?.username ?? '';
    if (account.may('profiles.manage'))
      tariffs = (await call(api.GET('/api/v1/tariffs')).catch(() => ({ items: [] }))).items;
    if (account.may('settings.manage')) {
      registration = await call(api.GET('/api/v1/system/registration')).catch(() => null);
      cleanup = registration?.cleanup_days ?? null;
    }
  }
  load();

  async function run(work: () => Promise<unknown>, done = '') {
    busy = true;
    failure = notice = '';
    try {
      await work();
      if (done) notice = t(done);
      return true;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      return false;
    } finally {
      busy = false;
    }
  }

  async function create(event: SubmitEvent) {
    event.preventDefault();
    const vpn = draft.vpn && account.may('profiles.manage');
    if (await run(async () => {
      created = await call(api.POST('/api/v1/invites', { body: { vpn, tariff_id: vpn && draft.tariff ? draft.tariff : null,
        days: draft.days, note: draft.note.trim() || null } }));
      items = (await call(api.GET('/api/v1/invites'))).items;
    })) creating = false;
  }

  const revoke = (id: string) => run(async () => {
    await call(api.DELETE('/api/v1/invites/{invite_id}', { params: { path: { invite_id: id } } }));
    revoking = null;
    items = (await call(api.GET('/api/v1/invites'))).items;
  }, 'people.invite_revoked_done');

  const saveBot = (event: SubmitEvent) => {
    event.preventDefault();
    return run(async () => {
      bot = await call(api.PUT('/api/v1/invites/telegram-bot', { body: { username: botName.trim().replace(/^@/, '') } }));
    }, 'people.saved');
  };

  const setRegistration = (changes: Schemas['RegistrationSettingsInput']) =>
    run(async () => {
      registration = await call(api.PATCH('/api/v1/system/registration', { body: changes }));
    }, 'people.saved');

  function what(item: Invite) {
    if (item.kind === 'account') return t('people.invite_kind_account');
    if (!item.vpn) return t('people.invite_kind_plain');
    const tariff = tariffs.find((entry) => entry.id === item.tariff_id);
    return tariff ? t('people.invite_kind_tariff', { tariff: tariff.title }) : t('people.invite_kind_vpn');
  }
</script>

<div class="page-head">
  <h1>{t('people.invites')}</h1>
  {#if account.may('accounts.manage') && !creating}
    <div class="actions"><button class="primary" onclick={() => { creating = true; created = null; }}>{t('people.invite_new')}</button></div>
  {/if}
</div>
{#if notice}<p class="muted">{notice}</p>{/if}
{#if failure}<p class="error">{failure}</p>{/if}

{#if creating}
  <section class="block">
    <h2>{t('people.invite_new')}</h2>
    <form class="form" onsubmit={create}>
      {#if account.may('profiles.manage')}
        <label class="check"><input type="checkbox" bind:checked={draft.vpn} /> {t('people.invite_vpn')}</label>
        {#if draft.vpn}
          <label>{t('people.invite_tariff')}
            <select bind:value={draft.tariff}>
              <option value="">{t('people.invite_no_tariff')}</option>
              {#each tariffs as tariff (tariff.id)}<option value={tariff.id}>{tariff.title}</option>{/each}
            </select>
          </label>
        {/if}
      {/if}
      <label>{t('people.invite_days')}
        <input type="number" min="1" max="30" bind:value={draft.days} />
      </label>
      <label>{t('people.invite_note')}
        <input maxlength="200" bind:value={draft.note} placeholder={t('people.invite_note_hint')} />
      </label>
      <div class="row">
        <button class="primary" disabled={busy || !(draft.days >= 1 && draft.days <= 30)}>{t('people.invite_make')}</button>
        <button type="button" onclick={() => (creating = false)}>{t('common.cancel')}</button>
      </div>
    </form>
  </section>
{/if}

{#if created}
  <div class="secret">
    <p><strong>{t('people.invite_once')}</strong></p>
    {#if created.cabinet_link}<p>{t('people.invite_cabinet')}: <code>{created.cabinet_link}</code></p>{/if}
    {#if created.telegram_link}
      <p>Telegram: <code>{created.telegram_link}</code></p>
      <p class="muted small">{t('people.invite_bot_bound', { bot: created.telegram_bot_username ?? '' })}</p>
    {/if}
    {#if !created.cabinet_link && !created.telegram_link}<p><code>{created.token}</code></p>{/if}
    <p class="muted small">{t('people.invite_expires', { time: dateTime(created.expires_at) })}</p>
  </div>
{/if}

{#if items}
  <section class="block">
    {#if !items.length}<p class="muted">{t('people.no_invites')}</p>{/if}
    <div class="rows">
      {#each items as item (item.id)}
        <div>
          <span class="grow"><strong>{item.note || what(item)}</strong>
            <span class="muted small">{item.note ? what(item) + ' · ' : ''}{t('people.invite_until', { date: date(item.expires_at) })}</span></span>
          <span class={'badge ' + (item.status === 'active' ? 'ok' : '')}>{t('people.invite_' + item.status)}</span>
          {#if item.status === 'active' && account.may('accounts.manage')}
            <button class="danger" onclick={() => (revoking = item.id)}>{t('people.revoke')}</button>
          {/if}
        </div>
        {#if revoking === item.id}
          <Confirm text={t('people.revoke_confirm')} action={t('people.revoke')} danger {busy}
            confirm={() => revoke(item.id)} cancel={() => (revoking = null)} />
        {/if}
      {/each}
    </div>
  </section>
{/if}

{#if bot?.telegram_enabled && account.may('settings.manage')}
  <section class="block">
    <h2>{t('people.bot_username')}</h2>
    <p class="muted small">{t('people.bot_username_hint')}</p>
    <form class="row" onsubmit={saveBot}>
      <input maxlength="33" bind:value={botName} placeholder="@my_vpn_bot" aria-label={t('people.bot_username')} />
      <button class="primary" disabled={busy || !botName.trim()}>{t('people.save')}</button>
    </form>
  </section>
{/if}

{#if registration}
  <section class="block">
    <h2>{t('people.registration')}</h2>
    <label class="check"><input type="checkbox" checked={registration.open} disabled={busy}
      onchange={(event) => setRegistration({ open: event.currentTarget.checked })} /> {t('people.registration_open')}</label>
    <p class="muted small">{t('people.registration_hint')}</p>
    <form class="row" onsubmit={(event) => { event.preventDefault();
      setRegistration({ cleanup_days: cleanup || null }); }}>
      <label>{t('people.registration_cleanup')}
        <input type="number" min="7" max="365" bind:value={cleanup} placeholder={t('people.registration_never')} />
      </label>
      <button disabled={busy}>{t('people.save')}</button>
    </form>
  </section>
{/if}

<style>
  .rows > div {
    padding: 0.4rem 0;
  }
  .small {
    font-size: 13px;
  }
  .grow .small {
    margin-left: 0.5rem;
  }
  .form {
    display: grid;
    gap: 0.75rem;
    max-width: 28rem;
  }
  .secret {
    margin: 0 0 1rem;
    padding: 0.6rem 0.9rem;
    border-left: 2px solid var(--accent);
    background: var(--panel);
    overflow-wrap: anywhere;
  }
  .secret p {
    margin: 0.2rem 0;
  }
</style>
