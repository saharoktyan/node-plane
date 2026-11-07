<!-- Temporary configurations: access to one server for half a day to three
     days, not tied to anyone's profile. A new one is made on the node in the
     background; once active it is shown as a link, a QR code and files, and
     can be revoked before it expires. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Config from '$lib/services/vpn/Config.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime, key } from '$lib/ui/format';

  type Temporary = { id: string; node_key: string; node_title?: string; protocol: 'awg' | 'xray'; transport: string;
    duration_seconds: number; status: string; created_at: string; expires_at: string | null; error_code: string | null;
    revocation_mode: string; artifact_available?: boolean; unavailable_reason?: string };
  type Node = { key: string; title: string; flag: string; enabled: boolean; protocols: string[]; xray_transports?: string[] };
  type Artifact = { content: string; files: { filename: string; content: string }[]; expires_at: string };

  const DURATIONS = [43_200, 86_400, 259_200];
  const WORKING = ['queued', 'issuing', 'revoking', 'expiry_pending'];

  let items = $state<Temporary[] | null>(null);
  let nodes = $state<Node[]>([]);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);
  let creating = $state(false);
  let draft = $state({ node: '', transport: '', duration: 86_400 });
  let revoking = $state<string | null>(null);
  let shown = $state<{ item: Temporary; artifact: Artifact } | null>(null);

  async function load() {
    try {
      items = ((await call(api.GET('/api/v1/system/temporary-configs', { params: { query: { page_size: 100 } } }))) as unknown as
        { items: Temporary[] }).items;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  // A node makes or removes one in the background: look again until it is done.
  const working = $derived(items?.some((item) => WORKING.includes(item.status)) ?? false);
  $effect(() => {
    if (!working) return;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  });

  async function open() {
    creating = true;
    notice = '';
    if (!nodes.length) {
      const page = await call(api.GET('/api/v1/nodes', { params: { query: { stage: 'node', limit: 100 } } })).catch(() => null);
      nodes = ((page?.items ?? []) as unknown as Node[]).filter((node) => node.enabled);
    }
  }

  const node = $derived(nodes.find((item) => item.key === draft.node));
  // Each protocol's transports on the chosen node, as protocol:transport.
  const transports = $derived(node ? [
    ...(node.protocols.includes('awg') ? ['awg:vpn', 'awg:conf'] : []),
    ...(node.protocols.includes('xray') ? (node.xray_transports ?? ['tcp']).map((item) => 'xray:' + item) : [])
  ] : []);
  const label = (protocol: string, transport: string) =>
    protocol === 'awg' ? 'AWG · ' + (transport === 'vpn' ? 'AmneziaVPN' : '.conf') : 'VLESS · ' + transport.toUpperCase();

  async function create(event: SubmitEvent) {
    event.preventDefault();
    const [protocol, transport] = draft.transport.split(':');
    if (!draft.node || !protocol) return;
    busy = true;
    failure = notice = '';
    try {
      await call(api.POST('/api/v1/system/temporary-configs', { params: { header: { 'Idempotency-Key': key() } },
        body: { node_key: draft.node, protocol, transport, duration_seconds: draft.duration } }));
      creating = false;
      draft = { node: '', transport: '', duration: 86_400 };
      notice = t('temporary.created');
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function show(item: Temporary) {
    busy = true;
    failure = '';
    try {
      const artifact = (await call(api.GET('/api/v1/system/temporary-configs/{config_id}/artifact',
        { params: { path: { config_id: item.id } } }))) as unknown as Artifact;
      shown = { item, artifact };
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function revoke() {
    if (!revoking) return;
    busy = true;
    failure = '';
    try {
      await call(api.POST('/api/v1/system/temporary-configs/{config_id}/revoke', { params: { path: { config_id: revoking } } }));
      if (shown?.item.id === revoking) shown = null;
      revoking = null;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const tone = (status: string) => (status === 'active' ? 'ok' : status.includes('blocked') ? 'danger' : 'warn');
</script>

<div class="page-head">
  <h1>{t('temporary.title')}</h1>
  {#if !creating}<div class="actions"><button class="primary" onclick={open}>{t('temporary.create')}</button></div>{/if}
</div>
<p class="muted small lead">{t('temporary.lead')}</p>
{#if notice}<p class="muted">{notice}</p>{/if}
{#if failure}<p class="error">{failure}</p>{/if}

{#if creating}
  <section class="block">
    <h2>{t('temporary.create')}</h2>
    <form class="form" onsubmit={create}>
      <label>{t('temporary.server')}
        <select bind:value={draft.node} onchange={() => (draft.transport = transports[0] ?? '')}>
          <option value="" disabled>—</option>
          {#each nodes as item (item.key)}<option value={item.key}>{item.flag} {item.title}</option>{/each}
        </select>
      </label>
      {#if node}
        <label>{t('temporary.protocol')}
          <select bind:value={draft.transport}>
            {#each transports as item (item)}<option value={item}>{label(item.split(':')[0], item.split(':')[1])}</option>{/each}
          </select>
        </label>
      {/if}
      <fieldset>
        <legend>{t('temporary.duration')}</legend>
        <div class="row">
          {#each DURATIONS as seconds (seconds)}
            <label class="check"><input type="radio" name="duration" value={seconds} bind:group={draft.duration} />
              {t('temporary.hours', { n: seconds / 3600 })}</label>
          {/each}
        </div>
      </fieldset>
      {#if draft.transport.startsWith('xray')}<p class="muted small">{t('temporary.vless_notice')}</p>{/if}
      <div class="row">
        <button class="primary" disabled={busy || !draft.node || !draft.transport}>{t('temporary.create')}</button>
        <button type="button" onclick={() => (creating = false)}>{t('common.cancel')}</button>
      </div>
    </form>
  </section>
{/if}

{#if shown}
  <div class="shown"><Config issuance={{ id: shown.item.id, node_key: shown.item.node_key, protocol: shown.item.protocol,
    transport: shown.item.transport, status: 'ready', expires_at: shown.artifact.expires_at, profile_id: '' } as Schemas['ConfigIssuanceOutput']}
    artifact={{ content: shown.artifact.content, files: shown.artifact.files, filename: shown.artifact.files[0]?.filename ?? null,
      media_type: 'text/plain', display_name: (shown.item.node_title ?? shown.item.node_key) + ' · ' + label(shown.item.protocol, shown.item.transport) }}
    onclose={() => (shown = null)} /></div>
{/if}

{#if items === null}
  {#if !failure}<p class="muted">{t('common.loading')}</p>{/if}
{:else if !items.length}
  <p class="muted">{t('temporary.empty')}</p>
{:else}
  <section class="block">
    <div class="rows">
      {#each items as item (item.id)}
        <div>
          <span class="grow"><strong>{item.node_title ?? item.node_key}</strong> · {label(item.protocol, item.transport)}
            <span class="sub muted small">{item.expires_at ? t('temporary.until_value', { time: dateTime(item.expires_at) }) : ''}
              <span class="badge {tone(item.status)}">{t('temporary.status_' + item.status)}</span>
              {#if item.unavailable_reason} · {t('temporary.unavailable_' + item.unavailable_reason)}{/if}</span></span>
          {#if item.status === 'active' && item.artifact_available !== false}
            <button disabled={busy} onclick={() => show(item)}>{t('temporary.show')}</button>
          {/if}
          {#if !['revoking', 'expired', 'revoked', 'cancelled'].includes(item.status)}
            <button class="danger" onclick={() => (revoking = item.id)}>{t('temporary.revoke')}</button>
          {/if}
        </div>
        {#if revoking === item.id}
          <Confirm text={t(item.protocol === 'xray' && item.revocation_mode !== 'connections_closed'
            ? 'temporary.revoke_confirm_vless' : 'temporary.revoke_confirm')} action={t('temporary.revoke')} danger {busy}
            confirm={revoke} cancel={() => (revoking = null)} />
        {/if}
      {/each}
    </div>
  </section>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .lead {
    margin-top: -0.5rem;
    max-width: 44rem;
  }
  .shown {
    margin-bottom: 1.5rem;
  }
  .form {
    display: grid;
    gap: 0.75rem;
    max-width: 30rem;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 0;
  }
  legend {
    margin-bottom: 0.25rem;
  }
  .rows > div {
    padding: 0.45rem 0;
    flex-wrap: wrap;
  }
  .sub {
    display: block;
    margin-top: 0.15rem;
  }
</style>
