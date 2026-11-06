<!-- Open registration, when the installation allows it: the browser solves
     the controller's puzzle, then the account is made without access. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { solve } from '$lib/pow';
  import { device, enter } from '$lib/signin';
  import Entry from '$lib/ui/Entry.svelte';
  import { errorText } from '$lib/ui/errors';

  let login = $state('');
  let password = $state('');
  let error = $state('');
  let progress = $state<string | null>(null);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    try {
      progress = t('auth.solving');
      const puzzle = await call(api.POST('/api/v1/registrations/challenge'));
      const nonce = await solve(puzzle.challenge, puzzle.bits);
      progress = null;
      const created = await call(api.POST('/api/v1/registrations', { body: {
        challenge_id: puzzle.challenge_id, nonce, login, password: password || null, device: device() } }));
      await enter(created.session);
    } catch (failure) {
      progress = null;
      error = failure instanceof ApiError ? errorText(failure) : t('common.error');
    }
  }
</script>

<Entry title={t('auth.register')}>
  <form onsubmit={submit}>
    <label>{t('auth.login')}<input bind:value={login} autocomplete="username" required /></label>
    <label>{t('auth.password_optional')}
      <input type="password" bind:value={password} autocomplete="new-password" /></label>
    <button class="primary" disabled={progress !== null}>{t('auth.create')}</button>
  </form>
  {#if progress}<p class="muted">{progress}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
  <p><a href="/login">{t('auth.have_account')}</a></p>
</Entry>
