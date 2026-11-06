<!-- Settings: language and theme; sign-in — the password, passkeys and
     the sessions of this account. -->
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
  let sessions = $state(fetchSessions());
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

  const endSession = (id: string) =>
    run(async () => {
      await call(api.DELETE('/api/v1/me/sessions/{session_id}', { params: { path: { session_id: id } } }));
      sessions = fetchSessions();
    }, 'settings.session_ended');
</script>

<h1>{t('settings.title')}</h1>
{#if notice}<p>{notice}</p>{/if}

<section class="card">
  <h2>{t('settings.appearance')}</h2>
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
</section>

<section class="card">
  <h2>{t('settings.password')}</h2>
  <form onsubmit={setPassword}>
    {#if account.me?.has_password}
      <label>{t('settings.current_password')}
        <input type="password" bind:value={current} autocomplete="current-password" required /></label>
    {/if}
    <label>{t('settings.new_password')}
      <input type="password" bind:value={password} autocomplete="new-password" required /></label>
    <button class="primary">{t('settings.save_password')}</button>
  </form>
</section>

<section class="card">
  <h2>{t('settings.passkeys')}</h2>
  <Async load={passkeys}>
    {#snippet children(items)}
      {#each items as item (item.id)}
        <div class="row line">
          <span>{item.name}</span>
          <span class="muted">{t('settings.used', { date: dateTime(item.used_at) })}</span>
          <button class="danger" onclick={() => removePasskey(item.id)}>{t('common.remove')}</button>
        </div>
      {:else}
        <p class="muted">{t('settings.no_passkeys')}</p>
      {/each}
    {/snippet}
  </Async>
  {#if webauthn.available()}<button onclick={addPasskey}>{t('settings.add_passkey')}</button>{/if}
</section>

<section class="card">
  <h2>{t('settings.sessions')}</h2>
  <Async load={sessions}>
    {#snippet children(items)}
      {#each items as item (item.id)}
        <div class="row line">
          <span>{item.device || t('settings.unknown_device')}</span>
          <span class="muted">{t('settings.method_' + item.method)} · {dateTime(item.used_at)}</span>
          {#if item.current}
            <span class="badge ok">{t('settings.this_device')}</span>
          {:else}
            <button class="danger" onclick={() => endSession(item.id)}>{t('settings.end_session')}</button>
          {/if}
        </div>
      {/each}
    {/snippet}
  </Async>
</section>

<style>
  section {
    margin-bottom: 1rem;
  }
  section h2 {
    margin-top: 0;
  }
  .line {
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--line);
  }
  .line > :first-child {
    flex: 1;
  }
</style>
