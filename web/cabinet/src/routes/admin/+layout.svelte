<!-- The administration: a section of its own, for staff. Its pages load
     only when opened, so members never download them. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { goto } from '$app/navigation';
  import { account } from '$lib/account.svelte';
  import { api, call } from '$lib/api/client';
  import { attention, refreshAttention } from '$lib/admin/attention.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Shell, { type Link } from '$lib/ui/Shell.svelte';
  import Confirmation from '$lib/admin/Confirmation.svelte';

  let { children: content }: { children: Snippet } = $props();
  // Sections follow the account's roles, as in the bot and Workstation.
  function adminSections() {
    const all: (Link & { allowed: boolean })[] = [
      { href: '/admin', label: t('admin.overview'), icon: 'overview', allowed: true },
      { href: '/admin/users', label: t('admin.users'), icon: 'users', group: t('admin.group_people'),
        allowed: account.may('accounts.manage') || account.may('access_requests.manage') || account.may('profiles.manage') },
      { href: '/admin/requests', label: t('people.requests'), icon: 'requests', group: t('admin.group_people'),
        allowed: account.may('access_requests.manage') || account.may('profiles.manage'), count: pending || undefined },
      { href: '/admin/invites', label: t('people.invites'), icon: 'invites', group: t('admin.group_people'),
        allowed: account.may('accounts.manage') },
      { href: '/admin/merges', label: t('people.merges'), icon: 'merges', group: t('admin.group_people'),
        allowed: account.may('accounts.manage') && account.may('profiles.manage') },
      { href: '/admin/nodes', label: t('admin.nodes'), icon: 'nodes', group: t('admin.group_infrastructure'), allowed: account.may('nodes.inspect') },
      { href: '/admin/services', label: t('admin.services'), icon: 'catalog', group: t('admin.group_infrastructure'), allowed: true },
      { href: '/admin/temporary-configs', label: t('temporary.title'), icon: 'temporary', group: t('admin.group_infrastructure'),
        allowed: account.may('settings.manage') },
      { href: '/admin/announcements', label: t('admin.announcements'), icon: 'announcements', group: t('admin.group_system'), allowed: account.may('settings.manage') },
      { href: '/admin/alerts', label: t('alerts.title'), icon: 'alerts', group: t('admin.group_system'), allowed: account.may('settings.manage'),
        count: attention.unacknowledged_alerts || undefined },
      { href: '/admin/backups', label: t('admin.backups'), icon: 'backups', group: t('admin.group_system'), allowed: account.may('settings.manage') },
      { href: '/admin/updates', label: t('admin.updates'), icon: 'updates', group: t('admin.group_system'), allowed: account.may('settings.manage'),
        dot: attention.update_available },
      { href: '/admin/system', label: t('admin.settings'), icon: 'system', group: t('admin.group_system'), allowed: account.may('settings.manage') }
    ];
    return all.filter((section) => section.allowed).map(({ allowed: _, ...section }) => section);
  }

  // Requests waiting for a decision: a count on Requests.
  let pending = $state(0);
  // The account at hand draws the administration at once; the counts follow.
  const ready = account.require().then(async (me) => {
    if (me && me.role !== 'admin') await goto('/');
    // Whether an update waits, a dot on Updates; unseen alerts, a count on Alerts.
    if (me?.role === 'admin' && account.may('settings.manage')) refreshAttention();
    if (me?.role === 'admin' && account.may('access_requests.manage'))
      call(api.GET('/api/v1/access-requests', { params: { query: { limit: 1 } } }))
        .then((value) => (pending += value.pending_total ?? 0)).catch(() => {});
    if (me?.role === 'admin' && account.may('profiles.manage'))
      call(api.GET('/api/v1/renewal-requests'))
        .then((value) => (pending += (value as { total?: number }).total ?? 0)).catch(() => {});
    return me;
  });
  const load = account.me ?? ready;
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
      <Confirmation />
    {/if}
  {/snippet}
</Async>
