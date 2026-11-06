<!-- The administration: a section of its own, for staff. Its pages load
     only when opened, so members never download them. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Shell from '$lib/ui/Shell.svelte';

  let { children: content }: { children: Snippet } = $props();
  // Sections follow the account's roles, as in the bot and Workstation.
  function adminSections() {
    const all = [
      { href: '/admin', label: t('admin.overview'), allowed: true },
      { href: '/admin/people', label: t('admin.people'),
        allowed: account.may('accounts.manage') || account.may('access_requests.manage') || account.may('profiles.manage') },
      { href: '/admin/nodes', label: t('admin.nodes'), allowed: account.may('nodes.inspect') },
      { href: '/admin/services', label: t('admin.services'), allowed: true },
      { href: '/admin/announcements', label: t('admin.announcements'), allowed: account.may('settings.manage') },
      { href: '/admin/system', label: t('admin.system'), allowed: account.may('settings.manage') }
    ];
    return all.filter((section) => section.allowed);
  }

  const load = account.require().then(async (me) => {
    if (me && me.role !== 'admin') await goto('/');
    return me;
  });
</script>

<Async {load}>
  {#snippet children(me)}
    {#if me && me.role === 'admin'}
      <Shell
        title={t('admin.title')}
        sections={adminSections()}
        switchTo={{ href: '/', label: t('common.cabinet') }}
      >
        {@render content()}
      </Shell>
    {/if}
  {/snippet}
</Async>
