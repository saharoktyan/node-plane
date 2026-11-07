<!-- The installation's own settings, as Workstation's Settings keep them:
     the controller's SSH key for servers, operations waiting for recovery
     anywhere, what Workstation sessions did, and cleaning the whole
     installation. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import Recovery from '$lib/admin/node/Recovery.svelte';
  import Cleanup from '$lib/admin/system/Cleanup.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';
  import Sections from '$lib/ui/Sections.svelte';
  import { toast } from '$lib/ui/toast.svelte';

  type Section = 'general' | 'recovery' | 'audit' | 'cleanup';
  type Entry = { title: string; occurred_at: string; account_label: string; ssh_user: string; action: string; phase: string };
  let section = $state<Section>('general');
  let sshKey = $state<string | null>(null);
  let audit = $state<{ items: Entry[]; total: number; offset: number; page_size: number } | null>(null);
  let errorsOnly = $state(false);
  let failure = $state('');

  call(api.GET('/api/v1/system/ssh-key')).then((value) => (sshKey = value.public_key)).catch((error) =>
    (failure = error instanceof ApiError ? errorText(error) : t('common.error')));
  async function loadAudit(offset = 0) {
    try {
      const page = (await call(api.GET('/api/v1/system/workstation-audit', { params: { query: { offset, errors_only: errorsOnly } } }))) as
        unknown as NonNullable<typeof audit>;
      audit = offset && audit ? { ...page, items: [...audit.items, ...page.items] } : page;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  loadAudit();

  async function copy() {
    if (!sshKey) return;
    await navigator.clipboard.writeText(sshKey).catch(() => {});
    toast(t('system.copied'));
  }
</script>

<div class="page-head"><h1>{t('admin.settings')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}
<section class="block appear">
  <Sections names={['general', 'recovery', 'audit', 'cleanup'] as Section[]} bind:current={section} label={t('admin.settings')}
    title={(name) => t('system.section_' + name)} />
  <div hidden={section !== 'general'}>
    <h3>{t('system.ssh_key')}</h3>
    <p class="muted small">{t('system.ssh_key_hint')}</p>
    {#if sshKey}<pre class="key">{sshKey}</pre><div class="row"><button onclick={copy}>{t('system.copy')}</button></div>{/if}
  </div>
  <div hidden={section !== 'recovery'}>
    <p class="muted small">{t('system.recovery_hint')}</p>
    <Recovery />
  </div>
  <div hidden={section !== 'audit'}>
    <div class="head">
      <p class="muted small">{t('system.audit_hint')}</p>
      <label class="check small"><input type="checkbox" bind:checked={errorsOnly} onchange={() => loadAudit()} /> {t('system.audit_errors')}</label>
    </div>
    {#if audit && !audit.items.length}<p class="muted">{t('system.audit_none')}</p>{/if}
    {#if audit}
      <div class="rows">
        {#each audit.items as entry, index (index)}
          <div><span class="grow"><strong>{entry.title || entry.action}</strong>
            <span class="muted small">{entry.account_label} · {entry.ssh_user} · {t('system.phase_' + entry.phase)}</span></span>
            <span class="muted small">{dateTime(entry.occurred_at)}</span></div>
        {/each}
      </div>
      {#if audit.offset + audit.page_size < audit.total}<p><button onclick={() => loadAudit(audit!.offset + audit!.page_size)}>
        {t('system.more')}</button></p>{/if}
    {/if}
  </div>
  <div hidden={section !== 'cleanup'}><Cleanup /></div>
</section>

<style>
  .small {
    font-size: 13px;
  }
  h3 {
    margin: 1rem 0 0.25rem;
  }
  .key {
    white-space: pre-wrap;
    word-break: break-all;
    font-family: var(--mono, monospace);
    font-size: 12px;
    background: var(--panel);
    padding: 0.75rem;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
  }
  .rows .grow {
    display: grid;
    gap: 0.15rem;
  }
</style>
