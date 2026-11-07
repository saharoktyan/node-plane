<!-- Updates, as the bot runs them: the controller's components and the
     newest version, a check now and on a schedule, the branch, every version
     to install or roll back to, and the job under way with its servers.
     Installing asks the session to confirm itself. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { refreshAttention } from '$lib/admin/attention.svelte';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime, key } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';
  import { toast } from '$lib/ui/toast.svelte';

  type Item = { node_key: string; status: string; error_code?: string | null; title?: string | null; flag?: string | null };
  type Job = { id: string; kind: string; status: string; created_at: string; target_ref?: string | null; items: Item[];
    result?: { components?: Record<string, string> } | null };
  type Overview = { branch: string; current_version: string; remote_version?: string | null; remote_label?: string | null;
    upstream_ref?: string | null; update_available: boolean; update_supported: boolean; last_checked_at?: string;
    auto_check_enabled: boolean; auto_check_interval_minutes: number; latest_job: Job | null; dismissed_job_ids: string[] };
  type Version = { version: string; ref: string; action: string; allowed: boolean; reason: string };
  type Versions = { branch: string; items: Version[]; next_offset: number | null; status?: string };

  const ACTIVE = ['awaiting_executor', 'running'];
  const COMPONENTS = ['backend', 'worker', 'driver', 'telegram'];
  let overview = $state<Overview | null>(null);
  let driver = $state('unknown');
  let versions = $state<Versions | null>(null);
  // What is about to be installed, with its key so a retry runs it once.
  let chosen = $state<{ body: { kind: 'stack' | 'version'; target_ref: string; branch: 'main' | 'dev' | 'beta' }; label: string; downgrade: boolean;
    key: string } | null>(null);
  let cancelling = $state(false);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      const [found, rollout] = await Promise.all([call(api.GET('/api/v1/system/updates')),
        call(api.GET('/api/v1/system/updates/rollout')).catch(() => null)]);
      overview = found as unknown as Overview;
      driver = (rollout as { driver_status?: string } | null)?.driver_status ?? 'unknown';
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  const job = $derived(overview?.latest_job ?? null);
  const active = $derived(!!job && ACTIVE.includes(job.status));
  // The job under way is followed; the controller may restart under it.
  $effect(() => {
    if (!active) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done?: string) {
    busy = true;
    failure = '';
    try {
      await work();
      if (done) toast(t(done));
      await load();
      await refreshAttention();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  const check = () => run(() => call(api.POST('/api/v1/system/updates/check')), 'updates.checked');
  const prefer = (changes: Record<string, unknown>) => run(async () => {
    await call(api.PATCH('/api/v1/system/updates/preferences', { body: changes as never }));
    versions = null;
  }, 'updates.saved');
  async function listVersions(offset = 0) {
    try {
      const page = (await call(api.GET('/api/v1/system/updates/versions', { params: { query: { offset } } }))) as unknown as Versions;
      versions = offset && versions ? { ...page, items: [...versions.items, ...page.items] } : page;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  function latest() {
    if (!overview?.upstream_ref) return;
    chosen = { body: { kind: 'stack', target_ref: overview.upstream_ref, branch: overview.branch as 'main' },
      label: overview.remote_label || overview.remote_version || overview.upstream_ref, downgrade: false, key: key() };
  }
  const choose = (item: Version) => (chosen = { body: { kind: 'version', target_ref: item.ref, branch: versions!.branch as 'main' },
    label: item.version, downgrade: item.action === 'downgrade', key: key() });
  const install = () => run(async () => {
    const body = chosen!.body;
    await confirmed(() => call(api.POST('/api/v1/system/updates/run', { headers: { 'Idempotency-Key': chosen!.key }, body })));
    chosen = null;
    versions = null;
  }, 'updates.started');
  const jobAction = (action: 'cancel' | 'recheck' | 'dismiss') => run(async () => {
    await call(api.POST(`/api/v1/system/updates/jobs/{job_id}/${action}` as '/api/v1/system/updates/jobs/{job_id}/cancel',
      { params: { path: { job_id: job!.id } } }));
    cancelling = false;
  });

  const label = (value: string) => t('updates.state_' + value);
  const component = (name: string) => (active && job?.result?.components?.[name])
    || (name === 'driver' ? driver : overview?.update_available ? 'required' : 'current');
  const failedNodes = $derived((job?.items ?? []).filter((item) => item.node_key !== '@driver'
    && ['blocked', 'failed', 'superseded'].includes(item.status)));
  const nodeName = (item: Item) => [item.flag, item.title ?? item.node_key].filter(Boolean).join(' ');
  const showResult = $derived(!!job && (active || !overview?.dismissed_job_ids.includes(job.id)));
</script>

<div class="page-head"><h1>{t('admin.updates')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}

{#if overview}
  <div class="columns appear">
    <div class="main">
      <section class="block">
        <div class="block-head"><h2>{t('updates.controller')}</h2>
          <span class="muted small">{overview.branch === 'main' ? 'stable' : overview.branch} · {overview.current_version}</span></div>
        <dl class="facts">
          {#each COMPONENTS as name (name)}
            {@const value = component(name)}
            <div><dt>{t('updates.component_' + name)}</dt><dd class={value}>{label(value)}</dd></div>
          {/each}
          <div><dt>{t('updates.newest')}</dt><dd>{overview.remote_label || overview.remote_version || '—'}
            <span class="muted"> · {overview.last_checked_at ? t('updates.checked_at', { time: dateTime(overview.last_checked_at) })
              : t('updates.not_checked')}</span></dd></div>
        </dl>
        <div class="row actions">
          {#if active}
            <span class="live">{t('updates.running')}</span>
          {:else if overview.update_supported && overview.update_available}
            <button class="primary" disabled={busy} onclick={latest}>{t('updates.to_latest')}</button>
          {/if}
          <button disabled={busy} onclick={check}>{t('updates.check')}</button>
          <button disabled={busy || active} onclick={() => (versions ? (versions = null) : listVersions())}>
            {t(versions ? 'updates.hide_versions' : 'updates.versions')}</button>
        </div>

        {#if chosen}
          <div class="confirm-install" transition:unfold>
            <p><strong>{t('updates.target', { value: chosen.label })}</strong></p>
            <p class="muted small">{t(chosen.body.kind === 'stack' ? 'updates.stack_warning'
              : chosen.downgrade ? 'updates.downgrade_warning' : 'updates.version_warning')}</p>
            <div class="row">
              <button class="primary" class:danger={chosen.downgrade} disabled={busy} onclick={install}>{t('updates.install')}</button>
              <button onclick={() => (chosen = null)}>{t('common.cancel')}</button>
            </div>
          </div>
        {/if}

        {#if versions}
          <div class="versions" transition:unfold>
            {#if versions.status && versions.status !== 'ok'}<p class="error small">{t('updates.catalog_failed')}</p>{/if}
            {#if !versions.items.length}<p class="muted">{t('updates.no_versions')}</p>{/if}
            <div class="rows">
              {#each versions.items as item (item.ref)}
                <div>
                  <span class="grow"><strong>{item.version}</strong>
                    <span class="muted small">{t('updates.reason_' + (item.reason || 'unrecognized_version'))}</span></span>
                  {#if item.action === 'current'}<span class="muted small">{t('updates.installed')}</span>
                  {:else if item.allowed}<button class:danger={item.action === 'downgrade'} disabled={busy} onclick={() => choose(item)}>
                    {t(item.action === 'downgrade' ? 'updates.roll_back' : 'updates.install')}</button>{/if}
                </div>
              {/each}
            </div>
            {#if versions.next_offset !== null}<p><button onclick={() => listVersions(versions!.next_offset!)}>{t('updates.more')}</button></p>{/if}
          </div>
        {/if}
      </section>

      {#if showResult && job}
        <section class="block">
          <div class="block-head"><h2>{t(active ? 'updates.progress' : 'updates.result')}</h2>
            <span class="muted small">{dateTime(job.created_at)}</span></div>
          <p><span class={job.status}>{label(job.status)}</span>{#if job.target_ref}<span class="muted"> · {job.target_ref}</span>{/if}</p>
          {#if ['partial', 'rolled_back', 'blocked'].includes(job.status)}<p class="muted small">{t('updates.note_' + job.status)}</p>{/if}
          {#if job.items.filter((item) => item.node_key !== '@driver').length}
            <dl class="facts">
              {#each job.items.filter((item) => item.node_key !== '@driver') as item (item.node_key)}
                <div><dt><a href={'/admin/nodes/' + item.node_key}>{nodeName(item)}</a></dt><dd class={item.status}>{label(item.status)}
                  {#if item.error_code}<span class="muted"> · {errorText(new ApiError(0, item.error_code, ''))}</span>{/if}</dd></div>
              {/each}
            </dl>
          {/if}
          <div class="row actions">
            {#if job.status === 'awaiting_executor'}
              <button class:armed={cancelling} class:danger={cancelling} disabled={busy}
                onclick={() => (cancelling ? jobAction('cancel') : (cancelling = true))}>{t('updates.cancel')}</button>
            {/if}
            {#if job.status === 'blocked' && job.kind === 'stack'}<button disabled={busy} onclick={() => jobAction('recheck')}>{t('updates.recheck')}</button>{/if}
            {#if !active}<button disabled={busy} onclick={() => jobAction('dismiss')}>{t('updates.dismiss')}</button>{/if}
            {#if cancelling}<Confirm text={t('updates.cancel_note')} action={t('updates.cancel')} cancel={() => (cancelling = false)} />{/if}
          </div>
        </section>
      {:else if failedNodes.length}
        <section class="block"><h2>{t('updates.failed_nodes')}</h2>
          <dl class="facts">{#each failedNodes as item (item.node_key)}<div><dt><a href={'/admin/nodes/' + item.node_key}>{nodeName(item)}</a></dt>
            <dd class="failed">{label(item.status)}</dd></div>{/each}</dl></section>
      {/if}
    </div>
    <aside>
      <section class="block">
        <h2>{t('updates.automatic')}</h2>
        <label class="check"><input type="checkbox" checked={overview.auto_check_enabled} disabled={busy}
          onchange={(event) => prefer({ auto_check_enabled: event.currentTarget.checked })} /> {t('updates.auto_check')}</label>
        {#if overview.auto_check_enabled}
          <label>{t('updates.every')}
            <select value={overview.auto_check_interval_minutes} disabled={busy}
              onchange={(event) => prefer({ auto_check_interval_minutes: Number(event.currentTarget.value) })}>
              {#each [60, 360, 1440] as minutes (minutes)}<option value={minutes}>{t('updates.interval_' + minutes)}</option>{/each}
            </select></label>
        {/if}
      </section>
      <section class="block">
        <h2>{t('updates.branch')}</h2>
        <select value={overview.branch} disabled={busy || active} aria-label={t('updates.branch')}
          onchange={(event) => prefer({ branch: event.currentTarget.value })}>
          {#each ['main', 'beta', 'dev'] as branch (branch)}<option value={branch}>{t('updates.branch_' + branch)}</option>{/each}
        </select>
        <p class="muted small">{t('updates.branch_hint')}</p>
      </section>
    </aside>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .actions {
    margin-top: 1rem;
  }
  .confirm-install,
  .versions {
    margin-top: 1rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--line);
  }
  .confirm-install p {
    margin: 0.25rem 0;
  }
  .rows .grow {
    display: grid;
    gap: 0.15rem;
  }
  .live,
  .running,
  .awaiting_executor {
    color: var(--accent);
  }
  .current,
  .succeeded {
    color: var(--ok);
  }
  .required,
  .blocked,
  .partial,
  .rolled_back {
    color: var(--warn);
  }
  .failed {
    color: var(--danger);
  }
  aside label {
    display: grid;
    gap: 0.3rem;
    margin-top: 0.75rem;
  }
  aside .check {
    display: flex;
  }
</style>
