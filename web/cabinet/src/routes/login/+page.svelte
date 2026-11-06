<!-- Sign in: login and password, or a passkey. Telegram comes with the
     Mini App and the Login Widget. -->
<script lang="ts">
  import { page } from '$app/state';
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { device, enter } from '$lib/signin';
  import Entry from '$lib/ui/Entry.svelte';
  import { errorText } from '$lib/ui/errors';
  import * as webauthn from '$lib/webauthn';

  let login = $state('');
  let password = $state('');
  let error = $state('');
  let busy = $state(false);
  const next = page.url.searchParams.get('next');
  const registration = call(api.GET('/api/v1/registrations')).catch(() => ({ open: false }));

  async function attempt(work: () => Promise<void>) {
    busy = true;
    error = '';
    try {
      await work();
    } catch (failure) {
      error = failure instanceof ApiError ? errorText(failure) : t('auth.passkey_cancelled');
    } finally {
      busy = false;
    }
  }

  const withPassword = (event: SubmitEvent) => {
    event.preventDefault();
    return attempt(async () => {
      await enter(await call(api.POST('/api/v1/sessions', { body: { login, password, device: device() } })), next);
    });
  };

  const withPasskey = () =>
    attempt(async () => {
      const { challenge_id, options } = await call(api.POST('/api/v1/sessions/passkey/options'));
      const credential = await webauthn.get(options);
      await enter(await call(api.POST('/api/v1/sessions/passkey', {
        body: { challenge_id, credential, device: device() } })), next);
    });
</script>

<Entry title={t('auth.sign_in')}>
  <form onsubmit={withPassword}>
    <label>{t('auth.login')}<input bind:value={login} autocomplete="username" required /></label>
    <label>{t('auth.password')}
      <input type="password" bind:value={password} autocomplete="current-password" required /></label>
    <div class="row">
      <button class="primary" disabled={busy}>{t('auth.sign_in')}</button>
      {#if webauthn.available()}
        <button type="button" onclick={withPasskey} disabled={busy}>{t('auth.with_passkey')}</button>
      {/if}
    </div>
  </form>
  {#if error}<p class="error">{error}</p>{/if}
  {#await registration then value}
    {#if value.open}<p><a href="/register">{t('auth.register')}</a></p>{/if}
  {/await}
</Entry>
