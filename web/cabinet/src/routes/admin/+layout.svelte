<!-- The administration: a section of its own, for staff. Its pages load
     only when opened, so members never download them. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Shell, { type Link } from '$lib/ui/Shell.svelte';

  let { children: content }: { children: Snippet } = $props();
  // Sections follow the account's roles, as in the bot and Workstation.
  function adminSections() {
    const all: (Link & { allowed: boolean })[] = [
      { href: '/admin', label: t('admin.overview'), icon: 'overview', allowed: true },
      { href: '/admin/users', label: t('admin.users'), icon: 'users', group: t('admin.group_people'),
        allowed: account.may('accounts.manage') || account.may('access_requests.manage') || account.may('profiles.manage') },
      { href: '/admin/nodes', label: t('admin.nodes'), icon: 'nodes', group: t('admin.group_infrastructure'), allowed: account.may('nodes.inspect') },
      { href: '/admin/services', label: t('admin.services'), icon: 'catalog', group: t('admin.group_infrastructure'), allowed: true },
      { href: '/admin/announcements', label: t('admin.announcements'), icon: 'announcements', group: t('admin.group_system'), allowed: account.may('settings.manage') },
      { href: '/admin/system', label: t('admin.system'), icon: 'system', group: t('admin.group_system'), allowed: account.may('settings.manage') }
    ];
    return all.filter((section) => section.allowed).map(({ allowed: _, ...section }) => section);
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
        switchTo={{ href: '/', label: t('common.cabinet'), icon: 'cabinet' }}
      >
        {@render content()}
      </Shell>
    {/if}
  {/snippet}
</Async>
