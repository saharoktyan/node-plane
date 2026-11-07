<script lang="ts">
  import type { Snippet } from 'svelte';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Shell from '$lib/ui/Shell.svelte';

  let { children: content }: { children: Snippet } = $props();
  // The account at hand draws the cabinet at once; otherwise it loads first.
  const load = account.me ?? account.require();
</script>

<Async {load}>
  {#snippet children(me)}
    {#if me}
      <Shell
        title={t('common.cabinet')}
        sections={[
          { href: '/', label: t('home.title'), icon: 'cabinet' },
          { href: '/inbox', label: t('inbox.title'), icon: 'inbox' }
        ]}
        switchTo={account.staff ? { href: '/admin', label: t('admin.title'), icon: 'admin' } : null}
      >
        {@render content()}
      </Shell>
    {/if}
  {/snippet}
</Async>
