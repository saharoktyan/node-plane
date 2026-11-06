<!-- Settings: what each group is on the left, its controls on the right —
     appearance, the password, passkeys, the sessions of this account and,
     for staff, the Workstations linked through the relay. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import { i18n, LANGUAGES, t } from '$lib/i18n/index.svelte';
  import { applyTheme, storedTheme, type Theme } from '$lib/theme';
  import Async from '$lib/ui/Async.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';
  import * as webauthn from '$lib/webauthn';

  let theme = $state<Theme>(storedTheme());
  let notice = $state('');
  let current = $state('');
  let password = $state('');

  const fetchSessions = async () => (await call(api.GET('/api/v1/me/sessions'))).items;
  const fetchPasskeys = async () => (await call(api.GET('/api/v1/me/passkeys'))).items;
  const fetchWorkstations = async () => (await call(api.GET('/api/v1/me/workstations'))).items;
  let sessions = $state(fetchSessions());
  let workstations = $state(account.staff ? fetchWorkstations() : Promise.resolve([]));
  let passkeys = $state(fetchPasskeys());

  async function run(work: () => Promise<unknown>, done: string) {
    notice = '';
    try {
      await work();
      notice = t(done);
    } catch (error) {
      notice = error instanceof ApiError ? errorText(error) : t('auth.passkey_cancelled');
    }
  }

  const setLanguage = (locale: 'ru' | 'en') =>
    run(async () => {
      account.me = await call(api.PATCH('/api/v1/me/preferences', { body: { locale } }));
      i18n.set(locale);
    }, 'settings.saved');

  const setPassword = (event: SubmitEvent) => {
    event.preventDefault();
    return run(async () => {
      account.me = await call(api.PUT('/api/v1/me/password', { body: {
        current_password: account.me?.has_password ? current : null, password } }));
      current = password = '';
      sessions = fetchSessions();
    }, 'settings.password_saved');
  };

  const addPasskey = () =>
    run(async () => {
      const { challenge_id, options } = await call(api.POST('/api/v1/me/passkeys/options'));
      const credential = await webauthn.create(options);
      await call(api.POST('/api/v1/me/passkeys', { body: { challenge_id, credential, name: null } }));
      passkeys = fetchPasskeys();
    }, 'settings.passkey_added');

  const removePasskey = (id: string) =>
    run(async () => {
      await call(api.DELETE('/api/v1/me/passkeys/{passkey_id}', { params: { path: { passkey_id: id } } }));
      passkeys = fetchPasskeys();
    }, 'settings.passkey_removed');

  const unlink = (id: string) =>
    run(async () => {
      await call(api.DELETE('/api/v1/me/workstations/{key_id}', { params: { path: { key_id: id } } }));
      workstations = fetchWorkstations();
    }, 'settings.workstation_unlinked');

  const endSession = (id: string) =>
    run(async () => {
      await call(api.DELETE('/api/v1/me/sessions/{session_id}', { params: { path: { session_id: id } } }));
      sessions = fetchSessions();
    }, 'settings.session_ended');
</script>

<div class="page-head"><h1>{t('settings.title')}</h1>{#if notice}<span class="muted">{notice}</span>{/if}</div>

<div class="setting">
  <div><h2>{t('settings.appearance')}</h2><p class="muted">{t('settings.appearance_hint')}</p></div>
  <div>
    <label>{t('settings.language')}
      <select value={i18n.language} onchange={(event) => setLanguage(event.currentTarget.value as 'ru' | 'en')}>
        {#each LANGUAGES as language (language)}<option value={language}>{t('settings.language_' + language)}</option>{/each}
      </select>
    </label>
    <label>{t('settings.theme')}
      <select bind:value={theme} onchange={() => applyTheme(theme)}>
        {#each ['system', 'light', 'dark'] as value (value)}<option {value}>{t('settings.theme_' + value)}</option>{/each}
      </select>
    </label>
  </div>
</div>

<div class="setting">
  <div><h2>{t('settings.password')}</h2><p class="muted">{t('settings.password_hint')}</p></div>
  <form onsubmit={setPassword}>
    {#if account.me?.has_password}
      <label>{t('settings.current_password')}
        <input type="password" bind:value={current} autocomplete="current-password" required /></label>
    {/if}
    <label>{t('settings.new_password')}
      <input type="password" bind:value={password} autocomplete="new-password" required /></label>
    <button class="primary">{t('settings.save_password')}</button>
  </form>
</div>

<div class="setting">
  <div><h2>{t('settings.passkeys')}</h2><p class="muted">{t('settings.passkeys_hint')}</p></div>
  <div>
    <Async load={passkeys}>
      {#snippet children(items)}
        <div class="rows">
          {#each items as item (item.id)}
            <div>
              <span class="grow">{item.name}</span>
              <span class="muted">{t('settings.used', { date: dateTime(item.used_at) })}</span>
              <button class="danger" onclick={() => removePasskey(item.id)}>{t('common.remove')}</button>
            </div>
          {:else}
            <p class="muted">{t('settings.no_passkeys')}</p>
          {/each}
        </div>
      {/snippet}
    </Async>
    {#if webauthn.available()}<p class="after"><button onclick={addPasskey}>{t('settings.add_passkey')}</button></p>{/if}
  </div>
</div>

<div class="setting">
  <div><h2>{t('settings.sessions')}</h2><p class="muted">{t('settings.sessions_hint')}</p></div>
  <Async load={sessions}>
    {#snippet children(items)}
      <div class="rows">
        {#each items as item (item.id)}
          <div>
            <span class="grow">{item.device || t('settings.unknown_device')}
              <span class="muted">· {t('settings.method_' + item.method)} · {dateTime(item.used_at)}</span></span>
            {#if item.current}
              <span class="badge ok">{t('settings.this_device')}</span>
            {:else}
              <button class="danger" onclick={() => endSession(item.id)}>{t('settings.end_session')}</button>
            {/if}
          </div>
        {/each}
      </div>
    {/snippet}
  </Async>
</div>

{#if account.staff}
  <div class="setting">
    <div><h2>{t('settings.workstations')}</h2><p class="muted">{t('settings.workstations_hint')}</p></div>
    <div>
      <Async load={workstations}>
        {#snippet children(items)}
          <div class="rows">
            {#each items as item (item.id)}
              <div>
                <span class="grow">{item.name}</span>
                <span class="muted">{t('settings.used', { date: dateTime(item.last_used_at ?? item.created_at) })}</span>
                <button class="danger" onclick={() => unlink(item.id)}>{t('settings.unlink')}</button>
              </div>
            {:else}
              <p class="muted">{t('settings.no_workstations')}</p>
            {/each}
          </div>
        {/snippet}
      </Async>
      <p class="after"><a href="/pair">{t('settings.link_workstation')}</a></p>
    </div>
  </div>
{/if}

<style>
  /* An action under a list of rows keeps clear of the last rule. */
  .after {
    margin-top: 1rem;
  }
</style>
