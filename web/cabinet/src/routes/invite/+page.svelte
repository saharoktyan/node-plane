<!-- The backend puts invite tokens in the fragment, outside server request URLs. -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { replaceState } from '$app/navigation';
  import Invite from '$lib/auth/Invite.svelte';
  import Entry from '$lib/ui/Entry.svelte';
  import { t } from '$lib/i18n/index.svelte';

  let token = $state<string | null>(null);
  onMount(() => {
    token = page.url.hash.slice(1);
    // Keep it only in this page's memory while the person chooses their login.
    replaceState(page.url.pathname + page.url.search, page.state);
  });
</script>

{#if token}
  <Invite {token} />
{:else}
  <Entry title={t('auth.invite')}>
    {#if token !== null}<p class="error">{t('errors.invite_invalid')}</p>{/if}
  </Entry>
{/if}
