<!-- What can be done to a server's VPN, as the bot's node tools: reinstall,
     ports, repairs and cleanup. Each asks again with what it does; the
     queued job shows here until it ends, a port check with its ports. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import type { Node, Services } from '$lib/admin/nodes';
  import { actions, DANGER, type Action } from './tools';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';

  type Job = { id: string; action: string; status: string; error_code?: string | null;
    result?: { ports?: { port: number; protocol: string; status: string }[]; firewall?: string; error_code?: string } | null };
  let { node, services, busy: running, onchange }: {
    node: Node; services: Services | null; busy: boolean; onchange: () => Promise<void>;
  } = $props();

  let armed = $state<{ action: Action; key: string } | null>(null);
  let job = $state<Job | null>(null);
  let busy = $state(false);
  let failure = $state('');
  const groups = $derived(actions(node, services));
  const ACTIVE = ['awaiting_executor', 'queued', 'running'];

  async function start(action: Action, command: string) {
    busy = true;
    failure = '';
    try {
      job = (await call(api.POST('/api/v1/nodes/{node_key}/actions', { params: { path: { node_key: node.key },
        header: { 'idempotency-key': command } } as never, body: { action, revision: node.desired_revision } }))) as unknown as Job;
      armed = null;
      await onchange();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  // The job is followed until it ends; the card reads the server again then.
  $effect(() => {
    if (!job || !ACTIVE.includes(job.status)) return;
    const id = job.id;
    const timer = setTimeout(async () => {
      const next = (await call(api.GET('/api/v1/node-jobs/{job_id}' as never, { params: { path: { job_id: id } } } as never))
        .catch(() => null)) as Job | null;
      if (next && job?.id === id) {
        job = next;
        if (!ACTIVE.includes(next.status)) await onchange();
      }
    }, 2000);
    return () => clearTimeout(timer);
  });
</script>

{#if failure}<p class="error">{failure}</p>{/if}
{#if job}
  <div class="job" class:done={job.status === 'succeeded'}>
    <p><strong>{t('nodes.action_' + job.action)}</strong> · {t('nodes.op_' + job.status)}</p>
    {#if job.status === 'succeeded'}
      <p class="small">{t('nodes.done_' + job.action)}</p>
      {#if job.result?.ports?.length}
        <table>
          <thead><tr><th>{t('nodes.port')}</th><th>{t('nodes.port_protocol')}</th><th>{t('nodes.port_result')}</th></tr></thead>
          <tbody>{#each job.result.ports as item (item.port + item.protocol)}
            <tr><td>{item.port}</td><td>{item.protocol.toUpperCase()}</td><td>{t('nodes.port_state_' + item.status)}</td></tr>{/each}</tbody>
        </table>
      {/if}
      {#if job.result?.firewall === 'unmanaged'}<p class="muted small">{t('nodes.firewall_unmanaged')}</p>{/if}
    {:else if job.status === 'blocked'}
      <p class="small">{t('nodes.job_blocked')}</p>
    {:else if ['failed', 'rejected'].includes(job.status)}
      {@const code = job.error_code || job.result?.error_code}
      {#if code}<p class="error small">{errorText(new ApiError(0, code, ''))}</p>{/if}
    {/if}
  </div>
{/if}

{#if !groups.length}
  <p class="muted small">{t('nodes.actions_unavailable')}</p>
{:else}
  {#if running}<p class="muted small">{t('nodes.actions_busy')}</p>{/if}
  {#each groups as [group, list] (group)}
    <h3>{t('nodes.actions_' + group)}</h3>
    <div class="list">
      {#each list as action (action)}
        {@const on = armed?.action === action}
        <div class="line">
          <button class:armed={on} class:danger={on && DANGER.includes(action)} disabled={busy || running || (job != null && ACTIVE.includes(job.status))}
            onclick={() => (on ? start(action, armed!.key) : (armed = { action, key: key() }))}>{t('nodes.action_' + action)}</button>
          <span class="muted small">{t('nodes.effect_' + action)}</span>
        </div>
        {#if on}<Confirm text={t('nodes.effect_' + action)} action={t('nodes.action_' + action)} cancel={() => (armed = null)} />{/if}
      {/each}
    </div>
  {/each}
{/if}

<style>
  .small {
    font-size: 13px;
  }
  h3 {
    margin: 1rem 0 0.4rem;
  }
  .list {
    display: grid;
    gap: 0.4rem;
  }
  .line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 1rem;
  }
  .line button {
    flex: none;
  }
  .job {
    padding: 0.6rem 0.9rem;
    margin-bottom: 0.75rem;
    border-left: 2px solid var(--warn);
    background: var(--panel);
  }
  .job.done {
    border-left-color: var(--ok);
  }
  .job p {
    margin: 0 0 0.3rem;
  }
  table {
    margin: 0.4rem 0;
  }
</style>
