<!-- A person's VPN profile, opened from their card: its term and tariff,
     which servers it reaches, its devices, limits and quota, its traffic and
     how far the nodes have applied it. A staff member's own VPN never
     expires, so its term, freezing and deletion are not offered. -->
<script lang="ts">
  import { toast } from '$lib/ui/toast.svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { personName } from '$lib/admin/people';
  import Policies from '$lib/admin/Policies.svelte';
  import Access from '$lib/admin/profile/Access.svelte';
  import Quota from '$lib/admin/profile/Quota.svelte';
  import Term from '$lib/admin/profile/Term.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Devices from '$lib/services/vpn/Devices.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';

  type Profile = Schemas['ProfileOutput'];
  const accountId = $derived(page.params.id as string);
  let id = $state('');
  let profile = $state<Profile | null>(null);
  let summary = $state<Schemas['MemberProfileSummary'] | null>(null);
  let operation = $state<Schemas['OperationOutput'] | null>(null);
  let owner = $state<Schemas['AdminAccountOutput'] | null>(null);
  let devices = $state<Schemas['DeviceList'] | null>(null);
  let failure = $state('');
  let busy = $state(false);
  let confirming = $state<{ action: 'freeze' | 'unfreeze' | 'delete'; key: string } | null>(null);

  const path = () => ({ path: { profile_id: id } });
  // A staff member's VPN: permanent, kept with the account.
  const permanent = $derived(owner?.role === 'admin');

  async function load() {
    failure = '';
    try {
      owner = await call(api.GET('/api/v1/accounts/{account_id}', { params: { path: { account_id: accountId } } }));
      if (!owner.profile_id) {
        profile = null;
        failure = t('people.no_profile');
        return;
      }
      id = owner.profile_id;
      profile = await call(api.GET('/api/v1/profiles/{profile_id}', { params: path() }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      return;
    }
    const [loadedSummary, loadedOperation, loadedDevices] = await Promise.all([
      call(api.GET('/api/v1/profiles/{profile_id}/summary', { params: path() })).catch(() => null),
      call(api.GET('/api/v1/profiles/{profile_id}/operation', { params: path() })).catch(() => null),
      call(api.GET('/api/v1/profiles/{profile_id}/devices', { params: path() })).catch(() => null)
    ]);
    summary = loadedSummary;
    operation = loadedOperation ?? null;
    devices = loadedDevices;
  }

  $effect(() => {
    void accountId;
    load();
  });

  // The nodes apply a change for a while: look again until they are done.
  const syncing = $derived(operation != null && ['awaiting_executor', 'running'].includes(operation.status));
  $effect(() => {
    if (!syncing) return;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  });

  async function confirm() {
    if (!confirming || !profile) return;
    const { action, key: commandKey } = confirming;
    busy = true;
    failure = '';
    const headers = { 'Idempotency-Key': commandKey, 'If-Match': `"${profile.desired_revision}"` };
    try {
      if (action === 'delete') {
        await call(api.DELETE('/api/v1/profiles/{profile_id}', { params: { ...path(), header: headers } }));
        await goto('/admin/users/' + accountId);
        return;
      }
      await call(api.PATCH('/api/v1/profiles/{profile_id}', { params: { ...path(), header: headers },
        body: { frozen: action === 'freeze' } }));
      toast(t('profiles.done_' + action));
      confirming = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  // Only the servers it used this month: a profile may reach dozens.
  const used = $derived((summary?.nodes ?? []).map((node) => ({ node,
    total: (summary?.traffic?.nodes ?? []).filter((item) => item.node_key === node.key)
      .reduce((sum, item) => sum + item.uplink_bytes + item.downlink_bytes, 0) }))
    .filter((row) => row.total > 0).sort((a, b) => b.total - a.total));
</script>

<nav class="crumbs"><a href="/admin/users">{t('admin.users')}</a> /
  <a href={'/admin/users/' + accountId}>{owner ? personName(owner) : '…'}</a></nav>
{#if failure && !profile}<p class="error">{failure}</p>
{:else if profile}
  <div class="page-head">
    <h1>{t('profiles.title')}</h1>
    <div class="actions">
      {#if profile.deleting}<span class="badge warn">{t('profiles.deleting')}</span>
      {:else if profile.frozen}<span class="badge warn">{t('profiles.frozen')}</span>
      {:else if summary?.expired}<span class="badge danger">{t('profiles.expired')}</span>
      {:else}<span class="badge ok">{t('profiles.active')}</span>{/if}
    </div>
  </div>
  {#if failure}<p class="error">{failure}</p>{/if}

  <div class="columns">
    <div class="main">
      <section class="block">
        <h2>{t('profiles.summary')}</h2>
        <dl class="facts">
          <div><dt>{t('profiles.name')}</dt><dd>{profile.display_name}
            {#if permanent}<span class="muted">· {t('profiles.staff')}</span>{/if}</dd></div>
          <div><dt>{t('profiles.servers')}</dt><dd>{summary?.node_count ?? '—'}</dd></div>
          <div><dt>{t('profiles.devices')}</dt><dd>{devices ? devices.items.filter((item) => item.status === 'active').length : '—'}</dd></div>
          <div><dt>{t('profiles.configs')}</dt><dd>{summary ? t('profiles.configs_value', { count: summary.issued_count,
            last: date(summary.last_issued_at) }) : '—'}</dd></div>
          {#if operation}
            <div><dt>{t('profiles.sync')}</dt><dd>{t('profiles.sync_' + operation.status)}</dd></div>
          {/if}
        </dl>
        {#if operation && !['succeeded', 'no_targets', 'superseded'].includes(operation.status)}
          <ul class="tasks small">
            {#each operation.tasks.filter((task) => task.status !== 'succeeded') as task (task.id)}
              <li>{task.node_key} · {task.protocol === 'awg' ? 'AWG' : 'VLESS'} · {t('profiles.sync_' + task.status)}</li>
            {/each}
          </ul>
        {/if}
      </section>

      {#if !profile.deleting}
        <Term {profile} {summary} {permanent} onchange={load} />
        <Access {profile} {permanent} onchange={load} />
      {/if}

      {#if devices && !profile.deleting}
        <Devices profileId={profile.id} list={devices} onchange={load} limits />
      {/if}

      {#if !profile.deleting}
        <Policies subject={{ kind: 'profile', profileId: profile.id }} onchange={load} />
        <Quota profileId={profile.id} revision={profile.desired_revision} onchange={load} />
      {/if}

      {#if used.length}
        <section class="block">
          <h2>{t('profiles.traffic')} {#if summary?.traffic?.month}<small>{summary.traffic.month}</small>{/if}</h2>
          <div class="table"><table>
            <thead><tr><th>{t('admin.node')}</th><th>{t('profiles.protocols')}</th><th>{t('profiles.used')}</th></tr></thead>
            <tbody>
              {#each used as { node, total } (node.key)}
                <tr><td>{node.flag} {node.title}</td>
                  <td>{(['awg', 'xray'] as const).filter((p) => node.protocols.includes(p)).map((p) => (p === 'awg' ? 'AWG' : 'VLESS')).join(' · ')}</td>
                  <td>{bytes(total)}</td></tr>
              {/each}
            </tbody>
          </table></div>
        </section>
      {/if}
    </div>

    <aside>
      {#if account.may('profiles.manage') && !profile.deleting && !permanent}
        <section class="block">
          <h2>{t('profiles.state')}</h2>
          {#if profile.frozen}
            <p class="muted small">{t('profiles.frozen_hint')}</p>
            <button class="primary" onclick={() => (confirming = { action: 'unfreeze', key: key() })}>{t('profiles.unfreeze')}</button>
          {:else}
            <p class="muted small">{t('profiles.freeze_hint')}</p>
            <button onclick={() => (confirming = { action: 'freeze', key: key() })}>{t('profiles.freeze')}</button>
          {/if}
          <p class="muted small gap">{t('profiles.delete_hint')}</p>
          <button class="danger" onclick={() => (confirming = { action: 'delete', key: key() })}>{t('profiles.delete')}</button>
          {#if confirming}
            <Confirm text={t('profiles.' + confirming.action + '_confirm')} action={t('profiles.' + confirming.action)}
              danger={confirming.action !== 'unfreeze'} {busy} {confirm} cancel={() => (confirming = null)} />
          {/if}
        </section>
      {/if}
    </aside>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .gap {
    margin-top: 1.25rem;
  }
  .tasks {
    margin: 0.5rem 0 0;
    padding-left: 1.2rem;
  }
  h2 small {
    font-weight: 400;
    font-size: 0.9rem;
    margin-left: 0.5rem;
    color: var(--muted);
  }
</style>
