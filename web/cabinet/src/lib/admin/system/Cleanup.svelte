<!-- Cleaning the installation, as Workstation does it: reset it to empty
     or remove it, with or without cleaning its servers. A plan says what
     goes, its phrase is typed back, the session confirms itself, and the
     job is followed until the controller stops. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';

  type Job = { id: string; status: string; phase?: string | null; error_code?: string | null; action: string;
    items: { node_key: string; status: string; error_code?: string | null }[]; can_abort: boolean };
  type Overview = { supported: boolean; reason?: string | null; counts: Record<string, number>; latest_job: Job | null };
  type Plan = { id: string; action: string; cleanup_nodes: boolean; confirmation_phrase: string; nodes: { key: string; title: string }[];
    key: string };
  let overview = $state<Overview | null>(null);
  let action = $state<'reset' | 'remove'>('reset');
  let nodes = $state(true);
  let plan = $state<Plan | null>(null);
  let phrase = $state('');
  let failure = $state('');
  let busy = $state(false);
  const ACTIVE = ['queued', 'running', 'blocked', 'awaiting_shutdown'];

  async function load() {
    try {
      overview = (await call(api.GET('/api/v1/system/cleanup'))) as unknown as Overview;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();
  const job = $derived(overview?.latest_job ?? null);
  const active = $derived(!!job && ACTIVE.includes(job.status));
  $effect(() => {
    if (!active) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>) {
    busy = true;
    failure = '';
    try {
      await work();
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  const prepare = () => run(async () => {
    plan = { ...((await call(api.POST('/api/v1/system/cleanup/plans', { body: { action, cleanup_nodes: nodes } }))) as unknown as Plan),
      key: key() };
    phrase = '';
  });
  const start = () => run(async () => {
    await confirmed(() => call(api.POST('/api/v1/system/cleanup/jobs', { headers: { 'Idempotency-Key': plan!.key },
      body: { plan_id: plan!.id, confirmation_phrase: phrase } })));
    plan = null;
  });
  const step = (name: 'retry' | 'abort') => run(() => confirmed(() => call(api.POST(
    `/api/v1/system/cleanup/jobs/{job_id}/${name}` as '/api/v1/system/cleanup/jobs/{job_id}/retry', { params: { path: { job_id: job!.id } } }))));
</script>

{#if failure}<p class="error">{failure}</p>{/if}
{#if overview && !overview.supported}
  <p class="muted">{t('system.cleanup_unsupported')}</p>
{:else if overview}
  <p class="muted small">{t('system.cleanup_hint', { accounts: overview.counts.accounts ?? 0, profiles: overview.counts.profiles ?? 0,
    nodes: overview.counts.nodes ?? 0 })}</p>
  {#if job && (active || job.status === 'failed')}
    <dl class="facts">
      <div><dt>{t('system.cleanup_' + job.action)}</dt><dd class:live={active}>{t('system.job_' + job.status)}{#if job.phase}<span class="muted"> · {job.phase}</span>{/if}</dd></div>
      {#if job.error_code}<div><dt>{t('system.error')}</dt><dd class="danger">{errorText(new ApiError(0, job.error_code, ''))}</dd></div>{/if}
      {#each job.items as item (item.node_key)}
        <div><dt>{item.node_key}</dt><dd>{t('system.job_' + item.status)}</dd></div>
      {/each}
    </dl>
    {#if job.status === 'awaiting_shutdown'}<p class="muted small">{t('system.cleanup_shutdown')}</p>{/if}
    <div class="row actions">
      {#if job.status === 'blocked'}<button disabled={busy} onclick={() => step('retry')}>{t('system.retry')}</button>{/if}
      {#if job.can_abort}<button class="danger" disabled={busy} onclick={() => step('abort')}>{t('system.abort')}</button>{/if}
    </div>
  {:else if plan}
    <div class="plan" transition:unfold>
      <p><strong>{t('system.plan_' + plan.action)}</strong></p>
      <p class="muted small">{plan.cleanup_nodes ? t('system.plan_nodes', { nodes: plan.nodes.map((node) => node.title).join(', ') || '—' })
        : t('system.plan_no_nodes')}</p>
      <label>{t('system.phrase', { phrase: plan.confirmation_phrase })}
        <input bind:value={phrase} autocomplete="off" spellcheck="false" /></label>
      <div class="row">
        <button class="primary danger" disabled={busy || phrase !== plan.confirmation_phrase} onclick={start}>{t('system.cleanup_start')}</button>
        <button onclick={() => (plan = null)}>{t('common.cancel')}</button>
      </div>
    </div>
  {:else}
    <fieldset>
      <label class="check"><input type="radio" value="reset" bind:group={action} /> {t('system.cleanup_reset')}</label>
      <p class="muted small">{t('system.cleanup_reset_hint')}</p>
      <label class="check"><input type="radio" value="remove" bind:group={action} /> {t('system.cleanup_remove')}</label>
      <p class="muted small">{t('system.cleanup_remove_hint')}</p>
      <label class="check"><input type="checkbox" bind:checked={nodes} /> {t('system.cleanup_nodes')}</label>
    </fieldset>
    <div class="row actions"><button class="danger" disabled={busy} onclick={prepare}>{t('system.cleanup_prepare')}</button></div>
  {/if}
{/if}

<style>
  .small {
    font-size: 13px;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 1rem 0 0;
    display: grid;
    gap: 0.35rem;
  }
  fieldset p {
    margin: 0 0 0.5rem 1.6rem;
  }
  .actions {
    margin-top: 1rem;
  }
  .plan {
    display: grid;
    gap: 0.6rem;
    max-width: 34rem;
    margin-top: 1rem;
  }
  .plan p {
    margin: 0;
  }
  .plan label {
    display: grid;
    gap: 0.3rem;
  }
  .live {
    color: var(--accent);
  }
  .danger {
    color: var(--danger);
  }
</style>
