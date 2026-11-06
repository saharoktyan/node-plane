<script lang="ts">
  import type { Snippet } from 'svelte';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Shell from '$lib/ui/Shell.svelte';

  let { children: content }: { children: Snippet } = $props();
  const load = account.require();
</script>

<Async {load}>
  {#snippet children(me)}
    {#if me}
      <Shell
        title={t('common.cabinet')}
        sections={[
          { href: '/', label: t('services.title') },
          { href: '/inbox', label: t('inbox.title') },
          { href: '/settings', label: t('settings.title') }
        ]}
        switchTo={account.staff ? { href: '/admin', label: t('admin.title') } : null}
      >
        {@render content()}
      </Shell>
    {/if}
  {/snippet}
</Async>
