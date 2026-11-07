<!-- Removing a server, as the backend's removal saga runs it: full cleanup
     revokes access, removes the VPN and lets the agent remove itself; or
     only the record goes, leaving the server as it is. Each is asked again
     with what it does; a running removal is followed until it ends. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { installed, type Node } from '$lib/admin/nodes';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';

  type Snapshot = { node_key: string; status: string; removal_status?: string; cleanup_phase?: string | null;
    revocations_complete?: boolean; pending_tasks?: number; blocked_tasks?: number; affected_profiles?: number;
    error_code?: string | null; checked_by?: string | null };
  let { node }: { node: Node } = $props();
  let snapshot = $state<Snapshot | null>(null);
  let armed = $state<'full' | 'registry' | null>(null);
  let busy = $state(false);
  let failure = $state('');

  const path = () => ({ path: { node_key: node.key } });
  async function load() {
    try {
      snapshot = (await call(api.GET('/api/v1/nodes/{node_key}/removal', { params: path() }))) as unknown as Snapshot;
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) {
        failure = error instanceof ApiError ? errorText(error) : t('common.error');
        return;
      }
      snapshot = (await call(api.GET('/api/v1/nodes/{node_key}/maintenance', { params: path() }))
        .catch(() => ({ node_key: node.key, status: 'active' }))) as unknown as Snapshot;
    }
  }
  load();

  const removed = $derived(['removed', 'removed_registry_only', 'removed_unprovisioned'].includes(snapshot?.status ?? ''));
  const active = $derived(['queued', 'running', 'blocked'].includes(snapshot?.removal_status ?? ''));
  const blocked = $derived(snapshot?.removal_status === 'blocked');
  const agent = $derived(installed(node));
  const profiles = $derived(snapshot?.affected_profiles ?? 0);

  // Each new snapshot schedules the next look while the removal runs.
  $effect(() => {
    void snapshot;
    if (!active || blocked) return;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  });

  const phase = $derived.by(() => {
    const value = snapshot?.cleanup_phase ?? 'not_started';
    if (value === 'not_started' && !snapshot?.revocations_complete) return 'revoking';
    return ['not_started', 'preparing'].includes(value) ? 'preparing' : value === 'prepared' ? 'runtime'
      : value === 'runtime_deleted' ? 'agent' : ['uninstall_scheduled', 'uninstall_uncertain'].includes(value) ? 'verifying' : 'worker';
  });

  async function remove(mode: 'full' | 'registry') {
    busy = true;
    failure = '';
    try {
      if (mode === 'full') {
        snapshot = (await confirmed(() => call(api.POST('/api/v1/nodes/{node_key}/remove-step', { params: path(),
          body: { retry: blocked } })))) as unknown as Snapshot;
      } else {
        await confirmed(() => call(api.POST('/api/v1/nodes/{node_key}/retire-registry-only', { params: path(), body: {
          accept_unverified_runtime: true,
          reason: 'Administrator explicitly removed the server record through the web cabinet; remote runtime may remain.' } as never })));
        snapshot = { node_key: node.key, status: 'removed_registry_only' };
      }
      armed = null;
    } catch (error) {
      // A lost answer is read back, never sent again by itself.
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      await load();
    } finally {
      busy = false;
    }
  }
</script>

{#if failure}<p class="error">{failure}</p>{/if}
{#if removed && snapshot}
  <p>{t(snapshot.status === 'removed' ? (snapshot.checked_by === 'agent' ? 'nodes.removed_agent' : 'nodes.removed_verified')
    : snapshot.status === 'removed_unprovisioned' ? 'nodes.removed_unprovisioned' : 'nodes.removed_registry')}</p>
  {#if snapshot.status === 'removed_registry_only'}<p class="muted small">{t('nodes.registry_tunnels')}</p>{/if}
  <div class="row"><a class="button primary" href="/admin/nodes">{t('admin.nodes')}</a></div>
{:else if snapshot}
  {#if agent}
    <p>{t('nodes.removal_state', { value: t(blocked ? 'nodes.removal_blocked' : active ? 'nodes.removal_running'
      : snapshot.status === 'draining' ? 'nodes.removal_draining' : 'nodes.removal_ready') })}</p>
    {#if active || snapshot.status === 'draining'}
      <p class="muted small">{t('nodes.removal_phase_' + phase)} · {t('nodes.removal_tasks', { pending: snapshot.pending_tasks ?? 0,
        blocked: snapshot.blocked_tasks ?? 0 })}</p>
    {/if}
    {#if snapshot.error_code}
      <p class="error small">{t('nodes.removal_error_' + snapshot.error_code) === 'nodes.removal_error_' + snapshot.error_code
        ? errorText(new ApiError(0, snapshot.error_code, '')) : t('nodes.removal_error_' + snapshot.error_code)}</p>
    {/if}
  {:else}
    <p class="muted small">{t('nodes.removal_invitation')}</p>
  {/if}

  <div class="row">
    {#if agent && (!active || blocked)}
      <button class:armed={armed === 'full'} class:danger={armed === 'full'} disabled={busy}
        onclick={() => (armed === 'full' ? remove('full') : (armed = 'full'))}>{t(blocked ? 'nodes.removal_retry' : 'nodes.removal_full')}</button>
    {/if}
    <button class:armed={armed === 'registry'} class:danger={armed === 'registry'} disabled={busy}
      onclick={() => (armed === 'registry' ? remove('registry') : (armed = 'registry'))}>{t(agent ? 'nodes.removal_registry' : 'nodes.remove')}</button>
    {#if armed === 'full'}
      <Confirm text={[t('nodes.drain_grants', { count: profiles }), t('nodes.drain_runtime'), t('nodes.drain_agent'),
        t('nodes.drain_registry')].join(' ')} action={t(blocked ? 'nodes.removal_retry' : 'nodes.removal_full')} cancel={() => (armed = null)} />
    {:else if armed === 'registry'}
      <Confirm text={agent || node.setup_attempted ? [t('nodes.registry_grants', { count: profiles }), t('nodes.registry_runtime'),
        t('nodes.registry_tunnels')].join(' ') : t('nodes.registry_grants', { count: profiles })}
        action={t(agent ? 'nodes.removal_registry' : 'nodes.remove')} cancel={() => (armed = null)} />
    {/if}
  </div>
  {#if agent}<p class="muted small">{t('nodes.registry_use')}</p>{/if}
{/if}

<style>
  .small {
    font-size: 13px;
  }
</style>
