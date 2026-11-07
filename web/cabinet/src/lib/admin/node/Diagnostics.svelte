<!-- A server's diagnostics as npw shows them: the agent's checks, worst
     first, the live look at the protocols and the host, and below the
     operations on this server that wait for recovery. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import Recovery from './Recovery.svelte';
  import { byStatus, checkName } from './tools';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, dateTime } from '$lib/ui/format';

  type Check = { id: string; status: string; detail?: string };
  type Facts = { error_code?: string; checks?: Check[]; docker?: string; runtime_root?: string; xray_config?: string;
    awg_config?: string; runtime_version?: string | null; checked_at?: string | null;
    observation?: { response_ms: number; host_metrics: Record<string, number>;
      protocols: { protocol: string; ports?: { transport?: string; network?: string; port?: number }[] }[] } | null };

  let { node }: { node: string } = $props();
  let facts = $state<Facts | null>(null);
  let failure = $state('');
  let busy = $state(false);
  let recovery = $state<Recovery>();

  const AGENT = ['node_agent_unconfigured', 'node_agent_unavailable', 'driver_unavailable'];
  async function load() {
    busy = true;
    failure = '';
    try {
      facts = (await call(api.GET('/api/v1/nodes/{node_key}/diagnostics', { params: { path: { node_key: node } } }))) as Facts;
    } catch (error) {
      // Recovery stays at hand when the agent is gone or silent.
      if (error instanceof ApiError && AGENT.includes(error.code)) facts = { error_code: error.code };
      else failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  load();

  const checks = $derived(byStatus(facts?.checks ?? []));
  const count = (status: string) => checks.filter((check) => check.status === status).length;
  const name = (id: string) => {
    const known = checkName(id);
    if ('raw' in known) return known.raw;
    return (known.protocol ? (known.protocol === 'awg' ? 'AmneziaWG' : 'VLESS') + ' · ' : '') + t('nodes.' + known.key);
  };
  const tone = (status: string) => (status === 'ok' ? 'ok' : status === 'error' || status === 'missing' || status === 'invalid' ? 'danger' : 'warn');
  const metrics = $derived(facts?.observation?.host_metrics ?? {});
  const percent = (value: number | undefined) => (value == null ? '—' : `${Math.round(value)}%`);
</script>

<div class="head">
  <p class="muted small">{#if facts?.checks?.length}{t('nodes.diagnostic_summary', { errors: count('error'), warnings: count('warning') })}
    {:else if facts?.checked_at}{t('nodes.checked_at', { time: dateTime(facts.checked_at) })}{/if}</p>
  <button disabled={busy} onclick={() => { load(); recovery?.load(); }}>{t('nodes.check_again')}</button>
</div>
{#if failure}<p class="error">{failure}</p>{/if}

{#if facts?.error_code}
  <p class="status warn">{t('nodes.diagnostics_' + facts.error_code)}</p>
{:else if checks.length}
  <ul class="checks">
    {#each checks as check (check.id)}
      <li><span class="status {tone(check.status)}">{t('nodes.check_status_' + check.status)}</span>
        <span>{name(check.id)}</span>{#if check.detail}<span class="muted small">{check.detail}</span>{/if}</li>
    {/each}
  </ul>
{:else if facts}
  <ul class="checks">
    {#each [['docker', 'Docker'], ['runtime_root', t('nodes.check_runtime_root')], ['xray_config', 'VLESS'], ['awg_config', 'AmneziaWG']] as [key, label] (key)}
      {@const value = String(facts[key as keyof Facts] ?? 'unknown')}
      <li><span class="status {tone(value)}">{t('nodes.check_fact_' + value)}</span><span>{label}</span></li>
    {/each}
  </ul>
  <p class="muted small">{t('nodes.runtime_version', { version: facts.runtime_version || '—' })}</p>
{/if}

{#if facts?.observation}
  {@const observation = facts.observation}
  <dl class="facts">
    <div><dt>CPU</dt><dd>{percent(metrics.cpu_used_percent)}</dd></div>
    <div><dt>RAM</dt><dd>{percent(metrics.ram_used_percent)}{#if metrics.ram_total_bytes}<span class="muted">
      · {bytes(metrics.ram_total_bytes - (metrics.ram_available_bytes ?? 0))} / {bytes(metrics.ram_total_bytes)}</span>{/if}</dd></div>
    <div><dt>{t('nodes.disk')}</dt><dd>{metrics.disk_free_percent == null ? '—' : percent(100 - metrics.disk_free_percent)}{#if metrics.disk_total_bytes}<span class="muted">
      · {bytes(metrics.disk_total_bytes - (metrics.disk_free_bytes ?? 0))} / {bytes(metrics.disk_total_bytes)}</span>{/if}</dd></div>
    {#if metrics.load1 != null}
      <div><dt>{t('nodes.load')}</dt><dd>{[metrics.load1, metrics.load5, metrics.load15].map((v) => v ?? '—').join(' / ')}
        <span class="muted">· {t('nodes.cpus', { count: metrics.cpus ?? '—' })}</span></dd></div>
    {/if}
    {#each observation.protocols.filter((item) => item.ports?.length) as item (item.protocol)}
      <div><dt>{item.protocol === 'awg' ? 'AmneziaWG' : 'VLESS'}</dt><dd>{item.ports!.map((port) =>
        [port.transport, port.network].filter(Boolean).join('/').toUpperCase() + ' ' + (port.port ?? '—')).join(' · ')}</dd></div>
    {/each}
    <div><dt>{t('nodes.response')}</dt><dd>{observation.response_ms} ms</dd></div>
  </dl>
{/if}

<h3>{t('nodes.recovery')}</h3>
<Recovery {node} bind:this={recovery} />

<style>
  .small {
    font-size: 13px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }
  .head p {
    margin: 0;
  }
  .checks {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 1rem;
  }
  .checks li {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.75rem;
    padding: 0.35rem 0;
    border-bottom: 1px solid var(--line);
  }
  .checks .status {
    min-width: 9rem;
  }
  .checks .muted {
    flex-basis: 100%;
    padding-left: 9.75rem;
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
  h3 {
    margin: 1.25rem 0 0.5rem;
  }
  @media (max-width: 640px) {
    .checks .muted {
      padding-left: 0;
    }
    .checks .status {
      min-width: 0;
    }
  }
</style>
