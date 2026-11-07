<!-- The web cabinet's relay, as Workstation keeps it: its address, node,
     TLS and link, and its settings; changing or removing it asks the
     session to confirm itself, since the cabinet itself goes through it. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';
  import { TLS, linkState, relayBody, relayDraft, type Relay, type RelayDraft } from './relay';

  let { nodes }: { nodes: { key: string; title: string }[] } = $props();
  let relay = $state<Relay | null | undefined>(undefined);
  let draft = $state<RelayDraft | null>(null);
  let removing = $state(false);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      relay = ((await call(api.GET('/api/v1/relay'))) as unknown as { relay: Relay | null }).relay;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  // While the node applies a change, it is asked again.
  $effect(() => {
    load();
    const timer = setInterval(() => ['applying', 'removing'].includes(relay?.status ?? '') && load(), 5000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done: string) {
    busy = true;
    failure = '';
    try {
      await confirmed(work);
      toast(t(done));
      draft = null;
      removing = false;
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
  function save() {
    if (!draft) return;
    const result = relayBody(draft);
    if ('error' in result) {
      failure = t('infra.' + result.error);
      return;
    }
    run(() => call(api.PUT('/api/v1/relay', { body: result.body as never })), 'infra.relay_saved');
  }
  const remove = () => (removing ? run(() => call(api.DELETE('/api/v1/relay')), 'infra.relay_removing') : (removing = true));
  const title = (key: string) => nodes.find((node) => node.key === key)?.title ?? key;
  const tone = (status: string) => (status === 'active' ? 'ok' : status === 'failed' ? 'danger' : 'warn');
</script>

<p class="muted small">{t('infra.relay_hint')}</p>
{#if failure}<p class="error">{failure}</p>{/if}
{#if draft}
  <form class="fields" onsubmit={(event) => { event.preventDefault(); save(); }}>
    <label>{t('infra.relay_node')}
      <select bind:value={draft.node_key} disabled={!!relay}>
        <option value="" disabled>{t('infra.choose_node')}</option>
        {#each nodes as node (node.key)}<option value={node.key}>{node.title}</option>{/each}
      </select>
      {#if !nodes.length}<span class="muted small">{t('infra.no_agent_nodes')}</span>{/if}
    </label>
    <label>{t('infra.relay_domain')}<input bind:value={draft.domain} placeholder="cab.example.com" autocapitalize="off" /></label>
    <label>TLS
      <select bind:value={draft.tls} onchange={() => draft!.tls === 'acme' && (draft!.port = '443')}>
        {#each TLS as mode (mode)}<option value={mode}>{t('infra.tls_' + mode)}</option>{/each}
      </select>
    </label>
    {#if draft.tls !== 'acme'}
      <label>{t('infra.relay_port')}<input bind:value={draft.port} inputmode="numeric" /></label>
    {/if}
    {#if draft.tls === 'acme'}
      <label>{t('infra.relay_contact')}<input type="email" bind:value={draft.acme_contact} /></label>
      <p class="muted small">{t('infra.tls_acme_hint')}</p>
    {:else if draft.tls === 'files'}
      <label>{t('infra.relay_certificate')}<input bind:value={draft.certificate_path} placeholder="/etc/ssl/cab/fullchain.pem" /></label>
      <label>{t('infra.relay_key')}<input bind:value={draft.key_path} placeholder="/etc/ssl/cab/privkey.pem" /></label>
    {:else}
      <p class="muted small">{t('infra.tls_proxy_hint')}</p>
    {/if}
    {#if relay}<p class="status warn small">{t('infra.relay_change_warning')}</p>{/if}
    <div class="row">
      <button class="primary" disabled={busy}>{t('people.save')}</button>
      <button type="button" onclick={() => { draft = null; failure = ''; }}>{t('common.cancel')}</button>
    </div>
  </form>
{:else if relay === null}
  <p>{t('infra.relay_none')}</p>
  <div class="row"><button class="primary" onclick={() => (draft = relayDraft(null))}>{t('infra.relay_setup')}</button></div>
{:else if relay}
  <dl class="facts">
    <div><dt>{t('infra.address')}</dt><dd><a href={relay.url} target="_blank" rel="noreferrer">{relay.url}</a></dd></div>
    <div><dt>{t('infra.relay_node')}</dt><dd>{title(relay.node_key)}</dd></div>
    <div><dt>{t('infra.status')}</dt><dd><span class="status {tone(relay.status)}">{t('infra.status_' + relay.status)}</span></dd></div>
    <div><dt>TLS</dt><dd>{t('infra.tls_' + relay.tls)}</dd></div>
    <div><dt>{t('infra.link')}</dt><dd>{t('infra.link_' + linkState(relay))}{#if linkState(relay) === 'disconnected' && relay.link?.error}
      <span class="muted"> · {relay.link.error}</span>{/if}</dd></div>
    {#if relay.error_code}<div><dt>{t('infra.last_error')}</dt><dd class="status danger">{errorText(new ApiError(0, relay.error_code, ''))}</dd></div>{/if}
  </dl>
  {#if relay.status !== 'removing'}
    <div class="row actions">
      <button onclick={() => (draft = relayDraft(relay!))}>{t('infra.change')}</button>
      <button class:armed={removing} class:danger={removing} disabled={busy} onclick={remove}>{t('infra.relay_remove')}</button>
      {#if removing}<Confirm text={t('infra.relay_remove_confirm')} action={t('infra.relay_remove')}
        cancel={() => (removing = false)} />{/if}
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
