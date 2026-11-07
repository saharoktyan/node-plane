<!-- The Telegram bot as a service, as Workstation's Settings › Services keep
     it: its name in the menu and where it runs, on the controller or on a
     node beside the relay. Its token and switching it on stay in
     Workstation: secrets do not travel through the cabinet. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';

  type Placement = { node_key: string; status: string; applied_version?: string | null; version: string; error_code?: string | null };
  type Envelope = { placement: Placement | null; telegram_enabled: boolean; relay_node_key: string | null };
  let title = $state<string | null>(null);
  let editing = $state<string | null>(null);
  let value = $state<Envelope | null>(null);
  let nodes = $state<{ key: string; title: string }[]>([]);
  let picking = $state<string | null>(null);
  let returning = $state(false);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      value = (await call(api.GET('/api/v1/telegram-bot/placement'))) as unknown as Envelope;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  call(api.GET('/api/v1/system/bot-title')).then((found) => (title = found.title)).catch(() => {});
  call(api.GET('/api/v1/nodes', { params: { query: { limit: 100 } } })).then((list) => {
    nodes = (list.items as { key: string; title: string; flag?: string | null; stage?: string }[])
      .filter((node) => node.stage === 'node').map((node) => ({ key: node.key, title: [node.flag, node.title].filter(Boolean).join(' ') }));
  }).catch(() => {});
  $effect(() => {
    load();
    const timer = setInterval(() => ['applying', 'removing'].includes(value?.placement?.status ?? '') && load(), 5000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done: string) {
    busy = true;
    failure = '';
    try {
      await work();
      toast(t(done));
      picking = null;
      returning = false;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  async function rename() {
    const next = editing?.trim() ?? '';
    if (!next) return;
    await run(async () => {
      title = (await call(api.PATCH('/api/v1/system/bot-title', { body: { title: next } }))).title;
      editing = null;
    }, 'infra.bot_title_saved');
  }
  const place = (node: string) => run(() => confirmed(() => call(api.PUT('/api/v1/telegram-bot/placement',
    { body: { node_key: node } }))), 'infra.bot_moving');
  const back = () => (returning ? run(() => confirmed(() => call(api.DELETE('/api/v1/telegram-bot/placement'))),
    'infra.bot_returning') : (returning = true));
  const nodeTitle = (key: string) => nodes.find((node) => node.key === key)?.title ?? key;
  const tone = (status: string) => (status === 'active' ? 'ok' : status === 'failed' ? 'danger' : 'warn');
</script>

<nav class="crumbs"><a href="/admin/services">{t('admin.services')}</a></nav>
<div class="page-head"><h1>{t('infra.bot')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}

<div class="setting appear">
  <div><h2>{t('infra.bot_title')}</h2><p class="muted">{t('infra.bot_title_hint')}</p></div>
  <div class="controls">
    {#if editing !== null}
      <form onsubmit={(event) => { event.preventDefault(); rename(); }}>
        <input bind:value={editing} maxlength="64" aria-label={t('infra.bot_title')} />
        <div class="row">
          <button class="primary" disabled={busy || !editing.trim()}>{t('people.save')}</button>
          <button type="button" onclick={() => (editing = null)}>{t('common.cancel')}</button>
        </div>
      </form>
    {:else}
      <p class="value">{title ?? '—'}</p>
      <div class="row"><button disabled={title === null} onclick={() => (editing = title)}>{t('infra.change')}</button></div>
    {/if}
  </div>
</div>

{#if value}
  <div class="setting appear">
    <div><h2>{t('infra.bot_place')}</h2><p class="muted">{t('infra.bot_place_hint')}</p></div>
    <div class="controls">
      {#if !value.telegram_enabled}<p class="status warn">{t('infra.bot_disabled')}</p>{/if}
      {#if !value.placement}
        <p class="value">{t('infra.bot_on_controller')}</p>
        {#if !value.relay_node_key}
          <p class="muted small">{t('infra.bot_relay_first')} <a href="/admin/services/relay">Relay</a></p>
        {:else if picking !== null}
          <label>{t('infra.bot_node')}
            <select bind:value={picking}>
              <option value="" disabled>{t('infra.choose_node')}</option>
              {#each nodes as node (node.key)}<option value={node.key}>{node.title}</option>{/each}
            </select>
          </label>
          <div class="row">
            <button class="primary" disabled={busy || !picking} onclick={() => place(picking!)}>{t('infra.bot_move')}</button>
            <button onclick={() => (picking = null)}>{t('common.cancel')}</button>
          </div>
        {:else}
          <div class="row"><button onclick={() => (picking = '')}>{t('infra.bot_to_node')}</button></div>
        {/if}
      {:else}
        {@const placement = value.placement}
        <dl class="facts">
          <div><dt>{t('infra.bot_runs_on')}</dt><dd>{nodeTitle(placement.node_key)}</dd></div>
          <div><dt>{t('infra.status')}</dt><dd><span class="status {tone(placement.status)}">{t('infra.status_' + placement.status)}</span></dd></div>
          {#if placement.applied_version && placement.applied_version !== placement.version}
            <div><dt>{t('infra.version')}</dt><dd>{t('infra.version_applying', { applied: placement.applied_version })}</dd></div>
          {/if}
          {#if placement.error_code}<div><dt>{t('infra.last_error')}</dt><dd class="status danger">{errorText(new ApiError(0, placement.error_code, ''))}</dd></div>{/if}
        </dl>
        {#if placement.status !== 'removing'}
          <div class="row">
            {#if placement.status === 'failed'}<button disabled={busy} onclick={() => place(placement.node_key)}>{t('infra.retry')}</button>{/if}
            <button class:armed={returning} class:danger={returning} disabled={busy} onclick={back}>{t('infra.bot_back')}</button>
            {#if returning}<Confirm text={t('infra.bot_back_confirm')} action={t('infra.bot_back')} cancel={() => (returning = false)} />{/if}
          </div>
        {/if}
      {/if}
      <p class="muted small">{t('infra.bot_secrets')}</p>
    </div>
  </div>
{/if}

<style>
  .controls {
    display: grid;
    gap: 0.75rem;
    align-content: start;
  }
  .controls p,
  .controls dl {
    margin: 0;
  }
  form {
    display: grid;
    gap: 0.75rem;
  }
  .value {
    font-family: var(--serif);
    font-size: 1.2rem;
  }
  .small {
    font-size: 13px;
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
