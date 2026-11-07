<!-- The question before a dangerous action: the account's password or one
     of its passkeys, once more, for a few minutes. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Dialog from '$lib/ui/Dialog.svelte';
  import { errorText } from '$lib/ui/errors';
  import * as webauthn from '$lib/webauthn';
  import { answer, confirmation } from './confirmation.svelte';

  let ways = $state<{ password: boolean; passkeys: boolean } | null>(null);
  let password = $state('');
  let failure = $state('');
  let busy = $state(false);
  let settled = false;

  // Each question starts clean and asks how this account can confirm itself.
  $effect(() => {
    if (!confirmation.open) return;
    settled = false;
    password = '';
    failure = '';
    call(api.GET('/api/v1/me/confirmation')).then((status) => (ways = status)).catch(() => (ways = { password: true, passkeys: false }));
  });

  async function run(work: () => Promise<unknown>) {
    busy = true;
    failure = '';
    try {
      await work();
      settled = true;
      answer(true);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('confirmation.passkey_cancelled');
    } finally {
      busy = false;
    }
  }
  const withPassword = () => run(() => call(api.POST('/api/v1/me/confirmation', { body: { password } })));
  const withPasskey = () => run(async () => {
    const { challenge_id, options } = await call(api.POST('/api/v1/me/confirmation/passkey/options'));
    const credential = await webauthn.get(options);
    await call(api.POST('/api/v1/me/confirmation/passkey', { body: { challenge_id, credential } }));
  });
  // Closing the window is a no.
  const closed = (open: boolean) => {
    if (!open && !settled && confirmation.done) answer(false);
  };
</script>

<Dialog bind:open={() => confirmation.open, (open) => closed(open)} title={t('confirmation.title')}>
  <p class="muted">{t('confirmation.hint')}</p>
  {#if ways?.password}
    <form onsubmit={(event) => { event.preventDefault(); withPassword(); }}>
      <label>{t('confirmation.password')}
        <!-- svelte-ignore a11y_autofocus -->
        <input type="password" autocomplete="current-password" bind:value={password} autofocus /></label>
      <div class="row">
        <button class="primary" disabled={busy || !password}>{t('confirmation.confirm')}</button>
        {#if ways.passkeys && webauthn.available()}
          <button type="button" disabled={busy} onclick={withPasskey}>{t('confirmation.with_passkey')}</button>
        {/if}
      </div>
    </form>
  {:else if ways?.passkeys}
    <div class="row"><button class="primary" disabled={busy || !webauthn.available()} onclick={withPasskey}>
      {t('confirmation.with_passkey')}</button></div>
  {:else if ways}
    <p>{t('confirmation.no_way')}</p>
  {/if}
  {#if failure}<p class="error">{failure}</p>{/if}
</Dialog>

<style>
  form {
    display: grid;
    gap: 1rem;
  }
  .row {
    margin-top: 0.25rem;
  }
</style>
