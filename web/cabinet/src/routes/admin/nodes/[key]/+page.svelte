<!-- A server's card, as in the bot and Workstation: what it is, its agent
     and protocols, this month's traffic, the operation running on it, its
     setup, settings and limits. It looks again while something runs. -->
<script lang="ts">
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import Policies from '$lib/admin/Policies.svelte';
  import Install from '$lib/admin/node/Install.svelte';
  import Maintenance from '$lib/admin/node/Maintenance.svelte';
  import Settings from '$lib/admin/node/Settings.svelte';
  import { agentOf, installed, needsSetup, pendingSettings, protocolName, protocolStatus, stateKey,
    type Agent, type Node, type Overview, type Services } from '$lib/admin/nodes';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, key } from '$lib/ui/format';
  import { toast } from '$lib/ui/toast.svelte';

  type Operation = { id: string; status: string; action?: string; error_code?: string | null; error?: string | null;
    progress?: { stage: string } | null };
  type Enrollment = { status: string; expires_at?: string | null; error_code?: string | null; ssh_install?: string | null };

  const nodeKey = $derived(page.params.key as string);
  let node = $state<Node | null>(null);
  let overview = $state<Overview | null>(null);
  let services = $state<Services | null>(null);
  let agent = $state<Agent>('unknown');
  let enrollment = $state<Enrollment | null>(null);
  let failure = $state('');
  let panel = $state<'settings' | 'limits' | 'maintenance' | null>(null);
  let busy = $state(false);

  const path = () => ({ path: { node_key: nodeKey } });

  async function load() {
    try {
      const [loaded, summary] = await Promise.all([
        call(api.GET('/api/v1/nodes/{node_key}', { params: path() })),
        call(api.GET('/api/v1/nodes/{node_key}/overview', { params: path() })).catch(() => null)
      ]);
      node = loaded as unknown as Node;
      overview = summary;
      failure = '';
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
      return;
    }
    // The agent answers for the services; why it cannot tells its state.
    // An invitation has none yet.
    if (!installed(node)) {
      services = null;
      agent = 'missing';
    } else try {
      services = (await call(api.GET('/api/v1/nodes/{node_key}/services', { params: path() }))) as Services;
      agent = 'ready';
    } catch (error) {
      services = null;
      agent = agentOf(error instanceof ApiError ? error.code : 'unknown');
    }
    enrollment = !installed(node) || agent === 'missing'
      ? ((await call(api.GET('/api/v1/nodes/{node_key}/agent-enrollment', { params: path() })).catch(() => null)) as Enrollment | null)
      : null;
  }

  $effect(() => {
    void nodeKey;
    load();
  });

  const ACTIVE = ['awaiting_executor', 'queued', 'running', 'blocked'];
  // The operation the server is busy with, or the last one that ended badly.
  const operation = $derived.by(() => {
    const all = [['bootstrap', overview?.bootstrap], ['settings', overview?.settings_operation],
      ['rollout', overview?.agent_rollout], ['job', overview?.last_job]]
      .filter(([, value]) => value) as [string, Operation][];
    return all.find(([, value]) => ACTIVE.includes(value.status))
      ?? all.find(([, value]) => ['failed', 'rejected'].includes(value.status)) ?? null;
  });
  const watching = $derived(node != null && (stateKey(node, overview) === 'applying'
    || (operation != null && ACTIVE.includes(operation[1].status) && operation[1].status !== 'blocked')
    || enrollment?.status === 'waiting'));
  $effect(() => {
    if (!watching) return;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  });

  const setup = $derived(node != null && (!installed(node) || agent === 'missing'
    || (node.protocols.length > 0 && needsSetup(node, overview, agent, services))
    || (operation?.[0] === 'bootstrap' && ACTIVE.includes(operation[1].status))));

  async function apply() {
    if (!node) return;
    busy = true;
    try {
      await call(api.POST('/api/v1/nodes/{node_key}/apply-settings', { params: { ...path(),
        header: { 'Idempotency-Key': key(), 'If-Match': `"${node.desired_revision}"` } } }));
      toast(t('nodes.apply_started'));
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const tone = (value: string) => (['applied_unverified', 'running', 'succeeded'].includes(value) ? 'ok'
    : ['needs_attention', 'deletion_blocked', 'failed', 'unavailable', 'missing'].includes(value) ? 'danger' : 'warn');
  type Traffic = { status: string; month: string; total_bytes: number | null;
    items: { protocol: string; status?: string; uplink_bytes: number; downlink_bytes: number;
      inbounds?: Record<string, { uplink_bytes: number; downlink_bytes: number }> }[] };
  const traffic = $derived(overview?.traffic as Traffic | null | undefined);
  const amount = (value: number | null | undefined) => (value == null ? '—' : (traffic?.status === 'unknown' ? '≈ ' : '') + bytes(value));
</script>

<nav class="crumbs"><a href="/admin/nodes">{t('admin.nodes')}</a></nav>
{#if failure && !node}<p class="error">{failure}</p>
{:else if node}
  {@const state = stateKey(node, overview)}
  <div class="page-head">
    <h1>{node.flag} {node.title}</h1>
    <div class="actions"><span class="badge {tone(state)}">{t('nodes.state_' + state)}</span></div>
  </div>
  {#if failure}<p class="error">{failure}</p>{/if}

  <div class="columns">
    <div class="main">
      <section class="block">
        <h2>{t('nodes.summary')}</h2>
        <dl class="facts">
          <div><dt>{t('nodes.code')}</dt><dd class="code">{node.key}</dd></div>
          <div><dt>{t('nodes.field_region')}</dt><dd>{node.region}</dd></div>
          <div><dt>{t('nodes.agent')}</dt><dd>{t('nodes.agent_' + agent)}{#if node.agent?.version}{' · ' + node.agent.version}{/if}
            {#if node.agent?.method}<span class="muted"> · {t('nodes.method_' + node.agent.method)}</span>{/if}</dd></div>
          <div><dt>{t('nodes.connection')}</dt><dd>{node.transport ? t('nodes.transport_' + node.transport) : '—'}
            {#if node.ssh_target}<span class="muted"> · {node.ssh_target}</span>{/if}</dd></div>
          <div><dt>{t('nodes.protocols')}</dt><dd>{['awg', 'xray'].filter((p) => node!.protocols.includes(p)).map(protocolName).join(' · ') || t('nodes.no_protocols')}
            {#if node.xray_transports?.length}<span class="muted"> · VLESS {node.xray_transports.map((item) => item.toUpperCase()).join(' · ')}</span>{/if}</dd></div>
          {#if node.protocols.length && installed(node)}
            <div><dt>{t('nodes.runtime')}</dt><dd>
              Docker: {services ? t(services.docker ? 'nodes.docker_ready' : 'nodes.docker_missing') : t('nodes.status_unchecked')}
              {#each (['awg', 'xray'] as const).filter((p) => node!.protocols.includes(p)) as protocol (protocol)}
                {@const status = protocolStatus(node, services, protocol)}
                {' · ' + (protocol === 'awg' ? 'AWG' : 'VLESS') + ': '}<span class="status {tone(status)}">{t('nodes.status_' + status)}</span>
              {/each}
            </dd></div>
          {/if}
          {#if overview && overview.access_total}
            <div><dt>{t('nodes.access')}</dt><dd>{t('nodes.access_value', { ready: overview.ready, total: overview.access_total })}
              {#if overview.pending}<span class="muted"> · {t('nodes.access_pending', { count: overview.pending })}</span>{/if}
              {#if overview.failed + overview.attention}<span class="status danger"> · {t('nodes.access_failed', { count: overview.failed + overview.attention })}</span>{/if}
            </dd></div>
          {/if}
        </dl>
      </section>

      {#if operation}
        {@const [kind, value] = operation}
        <section class="block">
          <h2>{t('nodes.operation_' + kind)}</h2>
          <p><span class="badge {tone(value.status)}">{t('nodes.op_' + value.status)}</span>
            {#if value.progress?.stage}<span class="muted"> · {t('nodes.progress_' + value.progress.stage)}</span>{/if}</p>
          {#if value.error_code || value.error}<p class="error small">{value.error_code ? errorText(new ApiError(0, value.error_code, '')) : value.error}</p>{/if}
        </section>
      {/if}

      {#if setup && account.may('nodes.manage')}
        <Install {node} {overview} {agent} {enrollment} onchange={load} onsettings={() => (panel = 'settings')} />
      {/if}

      {#if panel === 'settings'}
        <Settings {node} oncancel={() => (panel = null)} onsaved={async () => { panel = null; toast(t('nodes.saved')); await load(); }} />
      {:else if panel === 'limits'}
        <section class="block">
          <div class="block-head"><h2>{t('nodes.limits')}</h2>
            <button onclick={() => (panel = null)}>{t('common.close')}</button></div>
          <p class="muted small">{t('nodes.limits_hint')}</p>
          <Policies subject={{ kind: 'installation', node: node.key, xray: node.protocols.includes('xray') }} bare />
        </section>
      {:else if panel === 'maintenance'}
        <Maintenance {node} {services} busy={operation != null && ACTIVE.includes(operation[1].status)} onchange={load}
          onclose={() => (panel = null)} />
      {/if}

      {#if traffic}
        <section class="block">
          <h2>{t('nodes.traffic')} <small>{traffic.month} · UTC</small></h2>
          <dl class="facts">
            <div><dt>{t('nodes.traffic_all')}</dt><dd>{amount(traffic.total_bytes)}</dd></div>
            <!-- VLESS above AWG; its transports to the left of its total. -->
            {#each [...traffic.items].sort((a, b) => (a.protocol === 'xray' ? -1 : b.protocol === 'xray' ? 1 : 0)) as item (item.protocol)}
              <div><dt>{item.protocol === 'awg' ? 'AWG' : 'VLESS'}</dt><dd>
                {#if item.inbounds && Object.keys(item.inbounds).length}
                  <span class="muted split">{Object.entries(item.inbounds).map(([name, value]) =>
                    (name === 'vless-tcp' ? 'TCP' : name === 'vless-xhttp' ? 'XHTTP' : name) + ' ' + amount(value.uplink_bytes + value.downlink_bytes)).join(' · ')}</span>
                {/if}{amount(item.uplink_bytes + item.downlink_bytes)}</dd></div>
            {/each}
          </dl>
          {#if traffic.status === 'unknown'}<p class="muted small">{t('nodes.traffic_partial')}</p>{/if}
        </section>
      {/if}
    </div>

    <aside>
      {#if account.may('nodes.manage')}
        <section class="block">
          <h2>{t('nodes.manage')}</h2>
          <div class="actions-list">
            <button onclick={() => (panel = panel === 'settings' ? null : 'settings')}>{t('nodes.settings')}</button>
            {#if node.protocols.length}
              <button onclick={() => (panel = panel === 'limits' ? null : 'limits')}>{t('nodes.limits')}</button>
            {/if}
            {#if pendingSettings(node) && !(operation && ACTIVE.includes(operation[1].status))}
              <button class="primary" disabled={busy} onclick={apply}>{t('nodes.apply')}</button>
            {/if}
            {#if installed(node) && node.applied_revision > 0 && node.protocols.length}
              <a class="button" href="/admin/temporary-configs">{t('temporary.title')}</a>
            {/if}
            <button onclick={() => (panel = panel === 'maintenance' ? null : 'maintenance')}>{t('nodes.maintenance')}</button>
          </div>
        </section>
      {/if}
    </aside>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .code {
    font-family: var(--mono, monospace);
  }
  .status.ok {
    color: var(--ok);
  }
  .status.danger {
    color: var(--danger);
  }
  .status.warn {
    color: var(--warn);
  }
  h2 small {
    font-weight: 400;
    font-size: 0.9rem;
    margin-left: 0.5rem;
    color: var(--muted);
  }
  .split {
    margin-right: 1rem;
    font-size: 13px;
  }
  .actions-list {
    display: grid;
    justify-items: start;
    gap: 0.35rem;
  }
</style>
