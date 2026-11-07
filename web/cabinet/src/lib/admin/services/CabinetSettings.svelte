<!-- Where the web cabinet is served from, as Workstation sets it: the
     relay's node beside the API, or the controller's own server. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';
  import { DEFAULT_LISTEN, cabinetBody } from './relay';

  type Cabinet = { mode: 'relay' | 'local'; listen?: string | null; url?: string | null; public_url?: string | null;
    status: string; applied_version?: string | null; version: string; error_code?: string | null };
  type Envelope = { cabinet: Cabinet | null; available: boolean; relay_node_key: string | null };
  let { onrelay }: { onrelay: () => void } = $props();
  let value = $state<Envelope | null>(null);
  let draft = $state<{ listen: string; public_url: string } | null>(null);
  let stopping = $state(false);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      value = (await call(api.GET('/api/v1/web-cabinet'))) as unknown as Envelope;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  $effect(() => {
    load();
    const timer = setInterval(() => ['applying', 'removing'].includes(value?.cabinet?.status ?? '') && load(), 5000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done: string) {
    busy = true;
    failure = '';
    try {
      await confirmed(work);
      toast(t(done));
      draft = null;
      stopping = false;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  const serve = (body: Record<string, unknown>) => run(() => call(api.PUT('/api/v1/web-cabinet', { body: body as never })),
    'infra.cabinet_saved');
  function save() {
    if (!draft) return;
    const result = cabinetBody(draft.listen, draft.public_url);
    if ('error' in result) failure = t('infra.' + result.error);
    else serve(result.body);
  }
  const stop = () => (stopping ? run(() => call(api.DELETE('/api/v1/web-cabinet')), 'infra.cabinet_stopping') : (stopping = true));
  const edit = (cabinet: Cabinet | null) => (draft = { listen: cabinet?.mode === 'local' && cabinet.listen ? cabinet.listen : DEFAULT_LISTEN,
    public_url: cabinet?.mode === 'local' ? (cabinet.public_url ?? '') : '' });
  const tone = (status: string) => (status === 'active' ? 'ok' : status === 'failed' ? 'danger' : 'warn');
</script>

<p class="muted small">{t('infra.cabinet_hint')}</p>
{#if failure}<p class="error">{failure}</p>{/if}
{#if value && !value.available}<p class="status warn">{t('infra.cabinet_unavailable')}</p>{/if}
{#if draft}
  <form class="fields" onsubmit={(event) => { event.preventDefault(); save(); }}>
    <p class="muted small">{t('infra.cabinet_local_hint')}</p>
    <label>{t('infra.cabinet_listen')}<input bind:value={draft.listen} /></label>
    <label>{t('infra.cabinet_url')}<input bind:value={draft.public_url} placeholder="https://panel.example.com" autocapitalize="off" /></label>
    <p class="status warn small">{t('infra.cabinet_warning')}</p>
    <div class="row">
      <button class="primary" disabled={busy}>{t('people.save')}</button>
      <button type="button" onclick={() => { draft = null; failure = ''; }}>{t('common.cancel')}</button>
    </div>
  </form>
{:else if value && !value.cabinet}
  <p>{t('infra.cabinet_none')}</p>
  {#if value.available}
    <div class="row">
      {#if value.relay_node_key}
        <button class="primary" disabled={busy} onclick={() => serve({ mode: 'relay' })}>{t('infra.cabinet_on_relay')}</button>
      {:else}
        <button onclick={onrelay}>{t('infra.relay_first')}</button>
      {/if}
      <button onclick={() => edit(null)}>{t('infra.cabinet_on_controller')}</button>
    </div>
  {/if}
{:else if value?.cabinet}
  {@const cabinet = value.cabinet}
  <dl class="facts">
    <div><dt>{t('infra.address')}</dt><dd>{#if cabinet.url}<a href={cabinet.url} target="_blank" rel="noreferrer">{cabinet.url}</a>{:else}—{/if}</dd></div>
    <div><dt>{t('infra.served_by')}</dt><dd>{cabinet.mode === 'relay' ? t('infra.served_relay')
      : t('infra.served_controller', { listen: cabinet.listen ?? '—' })}</dd></div>
    <div><dt>{t('infra.status')}</dt><dd><span class="status {tone(cabinet.status)}">{t('infra.status_' + cabinet.status)}</span></dd></div>
    {#if cabinet.applied_version && cabinet.applied_version !== cabinet.version}
      <div><dt>{t('infra.version')}</dt><dd>{t('infra.version_applying', { applied: cabinet.applied_version })}</dd></div>
    {/if}
    {#if cabinet.error_code}<div><dt>{t('infra.last_error')}</dt><dd class="status danger">{errorText(new ApiError(0, cabinet.error_code, ''))}</dd></div>{/if}
  </dl>
  {#if cabinet.mode === 'local'}<p class="status warn small">{t('infra.cabinet_warning')}</p>{/if}
  {#if cabinet.status !== 'removing'}
    <div class="row actions">
      {#if cabinet.mode === 'local'}<button onclick={() => edit(cabinet)}>{t('infra.change')}</button>{/if}
      <button class:armed={stopping} class:danger={stopping} disabled={busy} onclick={stop}>{t('infra.cabinet_stop')}</button>
      {#if stopping}<Confirm text={t('infra.cabinet_stop_confirm')} action={t('infra.cabinet_stop')}
        cancel={() => (stopping = false)} />{/if}
    </div>
  {/if}
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .fields {
    display: grid;
    gap: 0.9rem;
    max-width: 34rem;
    margin-top: 1rem;
  }
  .fields p {
    margin: 0;
  }
  .actions {
    margin-top: 1rem;
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
</style>
