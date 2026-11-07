<!-- Setting a server up, as the bot does: an agent first, from a one-time
     command run on the server as root, or run once by the controller over
     its own SSH access; then the VPN protocols. The controller host's own
     agent installs without a command. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { canBootstrap, installed, type Agent, type Node, type Overview } from '$lib/admin/nodes';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime, key } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';

  type Enrollment = { status: string; expires_at?: string | null; error_code?: string | null; ssh_install?: string | null };
  let { node, overview, agent, enrollment, onchange, onsettings }: {
    node: Node; overview: Overview | null; agent: Agent; enrollment: Enrollment | null;
    onchange: () => Promise<void>; onsettings: () => void;
  } = $props();

  let step = $state<null | { ssh: boolean }>(null);
  let restrict = $state(false);
  let command = $state<{ text: string; expires: string; restrict: boolean } | null>(null);
  let armed = $state<'bootstrap' | 'local' | null>(null);
  let busy = $state(false);
  let failure = $state('');
  let copied = $state(false);
  const commandKey = key();

  // The agent comes first: not installed yet, or gone from the server.
  const needsAgent = $derived(!installed(node) || agent === 'missing');
  const waiting = $derived(enrollment?.status === 'waiting' || ['queued', 'running'].includes(enrollment?.ssh_install ?? ''));

  async function run(work: () => Promise<unknown>) {
    busy = true;
    failure = '';
    try {
      await work();
      await onchange();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const issue = () => run(async () => {
    const issued = (await call(api.POST('/api/v1/nodes/{node_key}/agent-enrollment', { params: { path: { node_key: node.key },
      header: { 'idempotency-key': commandKey } }, body: { revision: node.desired_revision, restrict_to_controller: restrict,
      over_ssh: step?.ssh ?? false } }))) as unknown as { command: string; expires_at: string };
    command = step?.ssh ? null : { text: issued.command, expires: issued.expires_at, restrict };
    step = null;
  });

  const bootstrap = () => run(async () => {
    await call(api.POST('/api/v1/nodes/{node_key}/bootstrap', { params: { path: { node_key: node.key },
      header: { 'idempotency-key': key() } }, body: { action: 'bootstrap', revision: node.desired_revision } }));
    armed = null;
  });

  // Only the controller's own host installs its agent without a command.
  const local = () => run(async () => {
    await call(api.POST('/api/v1/nodes/{node_key}/agent-rollouts', { params: { path: { node_key: node.key },
      header: { 'Idempotency-Key': key() } }, body: { transport: 'local' } as never }));
    armed = null;
  });

  async function copy() {
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command.text);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch {
      copied = false;
    }
  }
</script>

<section class="block">
  <h2>{t(needsAgent ? 'nodes.install_agent' : 'nodes.install_protocols')}</h2>
  {#if failure}<p class="error">{failure}</p>{/if}

  {#if needsAgent && node.transport === 'local'}
    <p class="muted small">{t('nodes.local_hint')}</p>
    <div class="row">
      <button class="primary" class:armed={armed === 'local'} disabled={busy}
        onclick={() => (armed === 'local' ? local() : (armed = 'local'))}>{t('nodes.install_agent')}</button>
    </div>
    {#if armed === 'local'}<Confirm text={t('nodes.local_confirm')} action={t('nodes.install_agent')} cancel={() => (armed = null)} />{/if}
  {:else if needsAgent}
    {#if command}
      <div class="command" transition:unfold>
        <p>{t('nodes.command_run')}</p>
        <pre>{command.text}</pre>
        <div class="row">
          <button class="primary" onclick={copy}>{copied ? t('nodes.copied') : t('nodes.copy')}</button>
          <span class="muted small">{t('nodes.command_expires', { time: dateTime(command.expires) })}
            · {t(command.restrict ? 'nodes.port_restricted' : 'nodes.port_open')}</span>
        </div>
      </div>
    {/if}
    {#if waiting}
      <p class="muted small">{t(enrollment?.ssh_install && ['queued', 'running'].includes(enrollment.ssh_install)
        ? 'nodes.ssh_running' : 'nodes.waiting_agent')}</p>
    {:else if enrollment?.status === 'expired'}
      <p class="muted small">{t('nodes.command_expired')}</p>
    {:else}
      <p class="muted small">{t(node.ssh_target ? 'nodes.agent_hint_ssh' : 'nodes.agent_hint')}</p>
    {/if}
    {#if enrollment?.error_code}<p class="error small">{t('nodes.enrollment_' + enrollment.error_code)}</p>{/if}
    {#if step}
      <fieldset transition:unfold>
        <legend>{t('nodes.port_access')}</legend>
        <p class="muted small">{t('nodes.port_access_hint')}</p>
        <label class="check"><input type="radio" name="port" value={false} bind:group={restrict} /> {t('nodes.port_open_choice')}</label>
        <label class="check"><input type="radio" name="port" value={true} bind:group={restrict} /> {t('nodes.port_restrict_choice')}</label>
        <div class="row">
          <button class="primary" disabled={busy} onclick={issue}>{t(step.ssh ? 'nodes.install_ssh' : 'nodes.get_command')}</button>
          <button onclick={() => (step = null)}>{t('common.cancel')}</button>
        </div>
      </fieldset>
    {:else}
      <div class="row">
        <button class="primary" onclick={() => (step = { ssh: false })}>{t(command || waiting ? 'nodes.new_command' : 'nodes.get_command')}</button>
        {#if node.ssh_target}<button onclick={() => (step = { ssh: true })}>{t('nodes.install_ssh')}</button>{/if}
      </div>
    {/if}
  {:else if overview?.settings_complete}
    <p class="muted small">{t('nodes.protocols_hint')}</p>
    <div class="row">
      <button class="primary" class:armed={armed === 'bootstrap'} disabled={busy || !canBootstrap(node, overview) && agent !== 'ready'}
        onclick={() => (armed === 'bootstrap' ? bootstrap() : (armed = 'bootstrap'))}>{t('nodes.install_protocols')}</button>
    </div>
    {#if armed === 'bootstrap'}<Confirm text={t('nodes.bootstrap_confirm')} action={t('nodes.install_protocols')} cancel={() => (armed = null)} />{/if}
  {:else}
    <p class="muted small">{t('nodes.settings_incomplete')}</p>
    <div class="row"><button class="primary" onclick={onsettings}>{t('nodes.settings')}</button></div>
  {/if}
</section>

<style>
  .small {
    font-size: 13px;
  }
  .command {
    margin: 0.25rem 0 0.75rem;
    padding: 0.6rem 0.9rem;
    border-left: 2px solid var(--accent);
    background: var(--panel);
  }
  .command p {
    margin: 0 0 0.4rem;
  }
  pre {
    margin: 0 0 0.4rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 13px;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 0.5rem 0 0;
    display: grid;
    gap: 0.4rem;
  }
  fieldset p {
    margin: 0;
  }
</style>
