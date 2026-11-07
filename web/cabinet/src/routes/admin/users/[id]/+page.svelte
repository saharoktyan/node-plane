<!-- A person's card: who they are, how they sign in and what they may do.
     Staff change the login, block the account, sign it out everywhere, take
     its passkeys and Workstations away or give it a sign-in link; owners
     assign roles. Each change names the revision it was made on. -->
<script lang="ts">
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { STAFF_ROLES, personName, telegramOf } from '$lib/admin/people';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime, key } from '$lib/ui/format';

  type Account = Schemas['AdminAccountOutput'];
  type Action = 'block' | 'unblock' | 'sessions' | 'passkeys' | 'workstations' | 'roles';
  const id = $derived(page.params.id as string);
  let person = $state<Account | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let confirming = $state<{ action: Action; key: string } | null>(null);
  let login = $state<string | null>(null);
  type Role = (typeof STAFF_ROLES)[number];
  let roles = $state<Role[] | null>(null);
  let invite = $state<Schemas['CreatedInvite'] | null>(null);

  async function load() {
    failure = '';
    try {
      person = await call(api.GET('/api/v1/accounts/{account_id}', { params: { path: { account_id: id } } }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  $effect(() => {
    void id;
    load();
  });

  async function run(work: () => Promise<Account | void>, done: string) {
    busy = true;
    notice = failure = '';
    try {
      const result = await work();
      if (result) person = result;
      else await load();
      notice = t(done);
      return true;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      return false;
    } finally {
      busy = false;
    }
  }

  const edit = (body: Schemas['AccountEditInput'], commandKey = key()) =>
    call(api.PATCH('/api/v1/accounts/{account_id}', { params: { path: { account_id: id },
      header: { 'Idempotency-Key': commandKey, 'If-Match': `"${person?.revision}"` } }, body }));

  async function saveLogin(event: SubmitEvent) {
    event.preventDefault();
    if (!login?.trim()) return;
    const value = login.trim();
    if (await run(() => edit({ login: value }), 'people.login_saved')) login = null;
  }

  async function confirm() {
    if (!confirming) return;
    const { action, key: commandKey } = confirming;
    const path = { params: { path: { account_id: id } } };
    const done = await run(() => {
      if (action === 'block') return edit({ status: 'disabled' }, commandKey);
      if (action === 'unblock') return edit({ status: 'approved' }, commandKey);
      if (action === 'roles') return edit({ roles: roles ?? [] }, commandKey);
      if (action === 'sessions') return call(api.DELETE('/api/v1/accounts/{account_id}/sessions', path));
      if (action === 'passkeys') return call(api.DELETE('/api/v1/accounts/{account_id}/passkeys', path));
      return call(api.DELETE('/api/v1/accounts/{account_id}/workstations', path)).then(() => undefined);
    }, 'people.done_' + action);
    if (done) {
      confirming = null;
      if (action === 'roles') roles = null;
    }
  }

  const ask = (action: Action) => (confirming = { action, key: key() });

  async function signInLink() {
    await run(async () => {
      invite = await call(api.POST('/api/v1/invites', { body: { account_id: id, days: 7, vpn: false } }));
    }, 'people.invite_made');
  }

  function toggleRole(role: Role) {
    if (!roles) return;
    roles = roles.includes(role) ? roles.filter((item) => item !== role) : [...roles, role];
  }
</script>

<nav class="crumbs"><a href="/admin/users">{t('admin.users')}</a></nav>
{#if failure && !person}<p class="error">{failure}</p>
{:else if !person}<p class="muted">{t('common.loading')}</p>
{:else}
  <div class="page-head">
    <h1>{personName(person)}</h1>
    <div class="actions">
      {#if person.status === 'disabled'}<span class="badge danger">{t('people.blocked')}</span>{/if}
      {#if person.deleted}<span class="badge warn">{t('people.deleting')}</span>{/if}
    </div>
  </div>
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}

  <div class="columns">
    <div class="main">
      <section class="block">
        <h2>{t('people.who')}</h2>
        <dl class="facts">
          <div><dt>{t('auth.login')}</dt><dd>
            {#if login !== null}
              <form class="row" onsubmit={saveLogin}>
                <!-- svelte-ignore a11y_autofocus -->
                <input maxlength="32" aria-label={t('auth.login')} bind:value={login} autofocus />
                <button class="primary" disabled={busy || !login.trim()}>{t('people.save')}</button>
                <button type="button" onclick={() => (login = null)}>{t('common.cancel')}</button>
              </form>
              <p class="muted small">{t('people.login_hint')}</p>
            {:else}
              {person.login ?? '—'}
              {#if account.may('accounts.manage') && !person.deleted}
                <button class="bare link" onclick={() => (login = person?.login ?? '')}>{t('people.change')}</button>
              {/if}
            {/if}
          </dd></div>
          <div><dt>{t('admin.name')}</dt><dd>{person.display_name ?? '—'}</dd></div>
          <div><dt>Telegram</dt><dd>{telegramOf(person)}
            {#if person.first_name || person.last_name}<span class="muted">· {[person.first_name, person.last_name].filter(Boolean).join(' ')}</span>{/if}
          </dd></div>
          <div><dt>{t('people.language')}</dt><dd>{person.locale ?? person.language_code ?? '—'}</dd></div>
          <div><dt>{t('people.vpn')}</dt><dd>
            {#if person.profile_id}<a href={'/admin/profiles/' + person.profile_id}>{t('people.open_profile')}</a>
            {:else}{t('people.no_profile')}{/if}
          </dd></div>
        </dl>
      </section>

      <section class="block">
        <div class="block-head"><h2>{t('admin.roles')}</h2>
          {#if account.may('roles.manage') && roles === null && !person.deleted}
            <button onclick={() => (roles = [...(person?.roles ?? [])] as Role[])}>{t('people.change')}</button>
          {/if}
        </div>
        {#if roles !== null}
          <div class="roles">
            {#each STAFF_ROLES as role (role)}
              <label class="check"><input type="checkbox" checked={roles.includes(role)} onchange={() => toggleRole(role)} />
                <span><strong>{t('admin.role_' + role)}</strong><br /><span class="muted small">{t('people.role_hint_' + role)}</span></span>
              </label>
            {/each}
          </div>
          {#if confirming?.action === 'roles'}
            <Confirm text={roles.length ? t('people.roles_confirm', { roles: roles.map((r) => t('admin.role_' + r)).join(', ') })
              : t('people.roles_confirm_none')} action={t('people.save')} {busy} confirm={confirm}
              cancel={() => (confirming = null)} />
          {:else}
            <div class="row">
              <button class="primary" onclick={() => ask('roles')}>{t('people.save')}</button>
              <button onclick={() => (roles = null)}>{t('common.cancel')}</button>
            </div>
          {/if}
        {:else}
          <p>{(person.roles ?? []).map((role) => t('admin.role_' + role)).join(', ') || t('people.no_roles')}</p>
          {#if !account.may('roles.manage')}<p class="muted small">{t('people.roles_owner_only')}</p>{/if}
        {/if}
      </section>

      <section class="block">
        <h2>{t('people.sign_in')}</h2>
        <div class="rows">
          <div>
            <span class="grow">{t('people.password')}: {person.has_password ? t('people.set') : t('people.not_set')}
              · {t('people.passkeys', { count: person.passkeys })}</span>
            {#if person.passkeys && account.may('accounts.manage')}
              <button class="danger" onclick={() => ask('passkeys')}>{t('people.remove_passkeys')}</button>
            {/if}
          </div>
          {#if confirming?.action === 'passkeys'}
            <Confirm text={t('people.passkeys_confirm')} action={t('people.remove_passkeys')} danger {busy} {confirm}
              cancel={() => (confirming = null)} />
          {/if}
          <div>
            <span class="grow">{t('people.sessions', { count: person.sessions })}</span>
            {#if person.sessions && account.may('accounts.manage')}
              <button class="danger" onclick={() => ask('sessions')}>{t('people.end_sessions')}</button>
            {/if}
          </div>
          {#if confirming?.action === 'sessions'}
            <Confirm text={t('people.sessions_confirm')} action={t('people.end_sessions')} danger {busy} {confirm}
              cancel={() => (confirming = null)} />
          {/if}
          {#if person.workstations}
            <div>
              <span class="grow">{t('people.workstations', { count: person.workstations })}</span>
              {#if account.may('accounts.manage')}
                <button class="danger" onclick={() => ask('workstations')}>{t('people.unlink_workstations')}</button>
              {/if}
            </div>
            {#if confirming?.action === 'workstations'}
              <Confirm text={t('people.workstations_confirm')} action={t('people.unlink_workstations')} danger {busy}
                {confirm} cancel={() => (confirming = null)} />
            {/if}
          {/if}
        </div>
        {#if account.may('accounts.manage') && !person.deleted}
          <p class="after"><button onclick={signInLink} disabled={busy}>{t('people.sign_in_link')}</button></p>
          <p class="muted small">{t('people.sign_in_link_hint')}</p>
        {/if}
        {#if invite}
          <div class="secret">
            <p>{t('people.invite_once')}</p>
            {#if invite.cabinet_link}<p><strong>{t('people.invite_cabinet')}</strong> <code>{invite.cabinet_link}</code></p>{/if}
            {#if invite.telegram_link}<p><strong>Telegram</strong> <code>{invite.telegram_link}</code></p>{/if}
            {#if !invite.cabinet_link && !invite.telegram_link}<p><code>{invite.token}</code></p>{/if}
            <p class="muted small">{t('people.invite_expires', { time: dateTime(invite.expires_at) })}</p>
          </div>
        {/if}
      </section>
    </div>

    <aside>
      {#if account.may('accounts.manage') && !person.deleted && person.id !== account.me?.id}
        <section class="block">
          <h2>{t('people.access')}</h2>
          {#if person.status === 'disabled'}
            <p class="muted small">{t('people.blocked_hint')}</p>
            <button class="primary" onclick={() => ask('unblock')}>{t('people.unblock')}</button>
          {:else}
            <p class="muted small">{t('people.block_hint')}</p>
            <button class="danger" onclick={() => ask('block')}>{t('people.block')}</button>
          {/if}
          {#if confirming?.action === 'block' || confirming?.action === 'unblock'}
            <Confirm text={t('people.' + confirming.action + '_confirm')} action={t('people.' + confirming.action)}
              danger={confirming.action === 'block'} {busy} {confirm} cancel={() => (confirming = null)} />
          {/if}
        </section>
      {/if}
    </aside>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .link {
    margin-left: 0.5rem;
    text-decoration: underline;
  }
  .roles {
    display: grid;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }
  .rows > div {
    padding: 0.35rem 0;
  }
  .after {
    margin: 0.75rem 0 0.25rem;
  }
  .secret {
    margin-top: 0.75rem;
    padding: 0.6rem 0.9rem;
    border-left: 2px solid var(--accent);
    background: var(--panel);
    overflow-wrap: anywhere;
  }
  .secret p {
    margin: 0.2rem 0;
  }
</style>
