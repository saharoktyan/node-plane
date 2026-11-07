<!-- Backups, as the bot and Workstation keep them: one made now with an
     optional name, automatic ones on a schedule, and the catalog, where a
     backup is downloaded, restored or deleted. A file saved elsewhere comes
     back by import. Restoring, importing and deleting ask the session to
     confirm itself. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, dateTime, key } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';
  import { toast } from '$lib/ui/toast.svelte';

  type Job = { id: string; action: 'create' | 'restore'; status: string; phase?: string | null;
    result?: { status?: string; backup_id?: string; error_code?: string } | null };
  type Item = { id: string; created_at: string; app_version: string; kind: 'manual' | 'automatic'; name: string; size_bytes: number };
  type Detail = Item & { checksum: string; compatible: boolean; accounts: number; profiles: number; nodes: number };
  type Overview = { enabled: boolean; interval_hours: number; keep_count: number; count: number; size_bytes: number;
    last_job: Job | null };
  type Page = { items: Item[]; total: number; next_offset: number | null };

  let overview = $state<Overview | null>(null);
  let page = $state<Item[]>([]);
  let next = $state<number | null>(null);
  let open = $state<Detail | null>(null);
  let armed = $state<'restore' | 'delete' | null>(null);
  let naming = $state<string | null>(null);
  let failure = $state('');
  let busy = $state(false);
  let input = $state<HTMLInputElement>();
  const ACTIVE = ['awaiting_executor', 'running', 'blocked'];

  async function load() {
    try {
      const [found, catalog] = await Promise.all([call(api.GET('/api/v1/system/backups')),
        call(api.GET('/api/v1/system/backups/catalog', { params: { query: { offset: 0 } } }))]);
      overview = found as unknown as Overview;
      const list = catalog as unknown as Page;
      page = list.items;
      next = list.next_offset;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();
  async function more() {
    if (next === null) return;
    const list = (await call(api.GET('/api/v1/system/backups/catalog', { params: { query: { offset: next } } }))) as unknown as Page;
    page = [...page, ...list.items];
    next = list.next_offset;
  }

  // A job under way is followed until it ends; the catalog then reads again.
  $effect(() => {
    const job = overview?.last_job;
    if (!job || !ACTIVE.includes(job.status)) return;
    const timer = setInterval(async () => {
      const now = (await call(api.GET('/api/v1/system/backups/jobs/{job_id}', { params: { path: { job_id: job.id } } }))
        .catch(() => job)) as unknown as Job;
      if (!ACTIVE.includes(now.status)) await load();
      else if (overview) overview.last_job = now;
    }, 2000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done?: string) {
    busy = true;
    failure = '';
    try {
      await work();
      if (done) toast(t(done));
      armed = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const create = () => run(async () => {
    await call(api.POST('/api/v1/system/backups/jobs', { headers: { 'Idempotency-Key': key() },
      body: { action: 'create', name: naming?.trim() || null } }));
    naming = null;
  }, 'backups.creating');
  const prefer = (changes: Record<string, unknown>) =>
    run(() => call(api.PATCH('/api/v1/system/backups/preferences', { body: changes as never })), 'backups.saved');

  async function show(item: Item) {
    armed = null;
    if (open?.id === item.id) {
      open = null;
      return;
    }
    try {
      open = (await call(api.GET('/api/v1/system/backups/catalog/{backup_id}', { params: { path: { backup_id: item.id } } }))) as unknown as Detail;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  function restore(detail: Detail) {
    if (armed !== 'restore') return (armed = 'restore');
    const commandKey = key();
    run(() => confirmed(() => call(api.POST('/api/v1/system/backups/jobs', { headers: { 'Idempotency-Key': commandKey },
      body: { action: 'restore', backup_id: detail.id, checksum: detail.checksum } }))), 'backups.restoring');
  }
  function remove(detail: Detail) {
    if (armed !== 'delete') return (armed = 'delete');
    run(async () => {
      await confirmed(() => call(api.POST('/api/v1/system/backups/catalog/{backup_id}/delete',
        { params: { path: { backup_id: detail.id } }, body: { checksum: detail.checksum } })));
      open = null;
    }, 'backups.deleted');
  }
  async function download(item: Item) {
    try {
      const blob = await call(api.GET('/api/v1/system/backups/catalog/{backup_id}/export',
        { params: { path: { backup_id: item.id } }, parseAs: 'blob' })) as unknown as Blob;
      const url = URL.createObjectURL(blob);
      Object.assign(document.createElement('a'), { href: url, download: `node-plane-backup-${item.id}.json` }).click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  function upload(file: File | undefined) {
    if (!file) return;
    run(() => confirmed(() => call(api.POST('/api/v1/system/backups/import', { body: file as never,
      bodySerializer: (body: unknown) => body as BodyInit, headers: { 'Content-Type': 'application/json' } }))), 'backups.imported');
    if (input) input.value = '';
  }
  const title = (item: Item) => item.name || t('backups.kind_' + item.kind);
  const job = $derived(overview?.last_job ?? null);
</script>

<div class="page-head"><h1>{t('admin.backups')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}

{#if overview}
  <div class="columns appear">
    <div class="main">
      <section class="block">
        <div class="block-head"><h2>{t('backups.catalog')}</h2>
          <span class="muted small">{t('backups.summary', { count: overview.count, size: bytes(overview.size_bytes) })}</span></div>
        {#if !page.length}<p class="muted">{t('backups.none')}</p>{/if}
        <div class="rows">
          {#each page as item (item.id)}
            <button class="item" class:open={open?.id === item.id} onclick={() => show(item)}>
              <span class="grow"><strong>{title(item)}</strong>
                <span class="muted small">{dateTime(item.created_at)} · {item.app_version} · {bytes(item.size_bytes)}</span></span>
            </button>
            {#if open?.id === item.id}
              {@const detail = open}
              <div class="detail" transition:unfold>
                <dl class="facts">
                  <div><dt>{t('backups.contents')}</dt><dd>{t('backups.contents_value', { accounts: detail.accounts,
                    profiles: detail.profiles, nodes: detail.nodes })}</dd></div>
                  <div><dt>{t('backups.compatible')}</dt><dd class:warn={!detail.compatible}>
                    {t(detail.compatible ? 'backups.compatible_yes' : 'backups.compatible_no')}</dd></div>
                  <div><dt>SHA-256</dt><dd class="code">{detail.checksum.slice(0, 16)}…</dd></div>
                </dl>
                <div class="row">
                  <button onclick={() => download(detail)}>{t('backups.download')}</button>
                  {#if detail.compatible}
                    <button class:armed={armed === 'restore'} class:danger={armed === 'restore'} disabled={busy}
                      onclick={() => restore(detail)}>{t('backups.restore')}</button>
                  {/if}
                  <button class:armed={armed === 'delete'} class:danger={armed === 'delete'} disabled={busy}
                    onclick={() => remove(detail)}>{t('backups.delete')}</button>
                  {#if armed}<Confirm text={t('backups.' + armed + '_confirm', { title: title(detail) })}
                    action={t('backups.' + armed)} cancel={() => (armed = null)} />{/if}
                </div>
              </div>
            {/if}
          {/each}
        </div>
        {#if next !== null}<p><button onclick={more}>{t('backups.more')}</button></p>{/if}
      </section>
    </div>
    <aside>
      <section class="block">
        <h2>{t('backups.now')}</h2>
        {#if job && ACTIVE.includes(job.status)}
          <p class="live">{t('backups.job_' + job.action)} · {t('backups.status_' + job.status)}</p>
        {:else if job}
          <p class="muted small">{t('backups.last_job', { action: t('backups.job_' + job.action) })}
            <span class:error={job.status === 'failed'}>{t('backups.status_' + job.status)}</span></p>
        {/if}
        {#if naming !== null}
          <form onsubmit={(event) => { event.preventDefault(); create(); }}>
            <label>{t('backups.name')}<input bind:value={naming} maxlength="128" /></label>
            <div class="row"><button class="primary" disabled={busy}>{t('backups.create')}</button>
              <button type="button" onclick={() => (naming = null)}>{t('common.cancel')}</button></div>
          </form>
        {:else}
          <div class="row"><button class="primary" disabled={busy || (job !== null && ACTIVE.includes(job.status))}
            onclick={() => (naming = '')}>{t('backups.create')}</button>
            <button disabled={busy} onclick={() => input?.click()}>{t('backups.import')}</button></div>
          <input type="file" accept="application/json,.json" hidden bind:this={input}
            onchange={(event) => upload(event.currentTarget.files?.[0])} />
        {/if}
      </section>
      <section class="block">
        <h2>{t('backups.automatic')}</h2>
        <label class="check"><input type="checkbox" checked={overview.enabled} disabled={busy}
          onchange={(event) => prefer({ enabled: event.currentTarget.checked })} /> {t('backups.automatic_on')}</label>
        {#if overview.enabled}
          <label>{t('backups.every')}
            <select value={overview.interval_hours} disabled={busy} onchange={(event) => prefer({ interval_hours: Number(event.currentTarget.value) })}>
              {#each [6, 12, 24] as hours (hours)}<option value={hours}>{t('backups.hours', { count: hours })}</option>{/each}
            </select></label>
          <label>{t('backups.keep')}
            <select value={overview.keep_count} disabled={busy} onchange={(event) => prefer({ keep_count: Number(event.currentTarget.value) })}>
              {#each [5, 10, 20] as count (count)}<option value={count}>{count}</option>{/each}
            </select></label>
        {/if}
      </section>
    </aside>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .rows .item {
    display: flex;
    width: 100%;
    text-align: left;
    padding: 0.7rem 0;
    border-bottom: 1px solid var(--line);
    text-decoration: none;
    font-weight: 400;
  }
  .rows .item:hover,
  .rows .item.open {
    background: var(--panel);
  }
  .rows .grow {
    display: grid;
    gap: 0.15rem;
  }
  .rows > .detail {
    display: block;
    padding: 0 0 1rem;
    border-bottom: 1px solid var(--line);
  }
  .detail .row {
    margin-top: 0.75rem;
  }
  .warn {
    color: var(--warn);
  }
  .live {
    color: var(--accent);
  }
  aside label {
    display: grid;
    gap: 0.3rem;
    margin-top: 0.75rem;
  }
  aside .check {
    display: flex;
  }
  form {
    display: grid;
    gap: 0.75rem;
  }
</style>
