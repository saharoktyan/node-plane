<!-- An invite link: a new account (with the access its template gives) or
     an account's own sign-in, chosen here. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { device, enter } from '$lib/signin';
  import Async from '$lib/ui/Async.svelte';
  import Entry from '$lib/ui/Entry.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date } from '$lib/ui/format';

  let { token }: { token: string } = $props();
  const preview = $derived(call(api.POST('/api/v1/invites/preview', { body: { token } })));
  let login = $state('');
  let password = $state('');
  let error = $state('');

  async function accept(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    try {
      const accepted = await call(api.POST('/api/v1/invites/accept', { body: {
        token, login: login || null, password: password || null, device: device() } }));
      await enter(accepted.session);
    } catch (failure) {
      error = failure instanceof ApiError ? errorText(failure) : t('common.error');
    }
  }
</script>

<Entry title={t('auth.invite')}>
  <Async load={preview}>
    {#snippet children(invite)}
      <p class="muted">
        {invite.kind === 'account' ? t('auth.invite_account') : t('auth.invite_new')}
        {#if invite.vpn}{t('auth.invite_vpn', { tariff: invite.tariff ?? '—' })}{/if}
        {t('auth.invite_until', { date: date(invite.expires_at) })}
      </p>
      <form onsubmit={accept}>
        {#if !invite.login}
          <label>{t('auth.login')}<input bind:value={login} autocomplete="username" required /></label>
        {:else}
          <p>{t('auth.login')}: <strong>{invite.login}</strong></p>
        {/if}
        <label>{invite.has_password ? t('auth.password_new') : t('auth.password_optional')}
          <input type="password" bind:value={password} autocomplete="new-password" /></label>
        <button class="primary">{t('auth.accept')}</button>
      </form>
      {#if error}<p class="error">{error}</p>{/if}
    {/snippet}
  </Async>
</Entry>
