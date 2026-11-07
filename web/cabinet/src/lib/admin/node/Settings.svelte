<!-- A server's settings on tabs, as the bot and Workstation edit them. A new
     region that changes who reaches the server asks once more with the
     number of profiles affected; once saved, a server with VPN applies the
     settings at once. -->
<script lang="ts">
  import { unfold } from '$lib/ui/motion';
  import { api, call, ApiError } from '$lib/api/client';
  import { pendingSettings, type Node } from '$lib/admin/nodes';
  import { t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import { CHOICES, check, dirty, draft, editor, fieldsOf, toggle, type Editor, type FieldKey, type Tab } from './editor';

  let { node, onsaved, oncancel }: { node: Node; onsaved: (node: Node) => Promise<void>; oncancel: () => void } = $props();

  let form = $state<Editor | null>(null);
  let tab = $state<Tab>('general');
  let failure = $state('');
  let busy = $state(false);
  // A region change waiting for its confirmation, with the profiles it moves.
  let review = $state<{ body: Record<string, unknown>; affected: number } | null>(null);
  const keys = { save: key(), apply: key() };

  async function load() {
    try {
      const [source, defaults, services] = await Promise.all([
        call(api.GET('/api/v1/nodes/{node_key}', { params: { path: { node_key: node.key } } })),
        call(api.GET('/api/v1/system/installation-defaults')).catch(() => ({ settings: {} })),
        call(api.GET('/api/v1/nodes/{node_key}/services', { params: { path: { node_key: node.key } } })).catch(() => null)
      ]);
      const settings = { ...((defaults as { settings?: Record<string, unknown> }).settings ?? {}) };
      const found = (services as { awg_parameters?: Record<string, string> } | null)?.awg_parameters;
      if (found && Object.keys(found).length) settings.awg_parameters = found;
      form = editor(source as unknown as Parameters<typeof editor>[0], settings);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  const tabs = $derived<Tab[]>(form?.protocols.includes('awg') ? ['general', 'protocols', 'advanced', 'awg']
    : ['general', 'protocols', 'advanced']);
  const errors = $derived(Object.fromEntries((form ? fieldsOf(form, tab) : []).map((field) =>
    [field, form!.values[field] === form!.baseline[field] ? null : check(field, form!.values[field])])));

  async function save(confirmed = false) {
    if (!form) return;
    failure = '';
    const result = review && confirmed ? { body: review.body } : draft(form);
    if ('error' in result) {
      failure = t('nodes.error_' + result.error, { field: result.field ? t('nodes.field_' + result.field) : '' });
      if (result.field) tab = (['general', 'advanced', 'awg'] as Tab[]).find((item) => fieldsOf(form!, item).includes(result.field!)) ?? tab;
      return;
    }
    busy = true;
    try {
      const body = result.body as Record<string, unknown>;
      if (typeof body.region === 'string' && !confirmed) {
        const preview = (await call(api.GET('/api/v1/nodes/{node_key}/region-access-preview',
          { params: { path: { node_key: node.key }, query: { region: body.region } } }))) as unknown as { affected_profiles: number };
        if (preview.affected_profiles > 0) {
          review = { body, affected: preview.affected_profiles };
          return;
        }
      }
      const saved = (await call(api.PATCH('/api/v1/nodes/{node_key}', { params: { path: { node_key: node.key },
        header: { 'Idempotency-Key': keys.save, 'If-Match': `"${form.source.desired_revision}"` } },
        body: { ...body, confirm_access_change: confirmed } as never }))) as unknown as Node;
      if (pendingSettings(saved)) {
        await call(api.POST('/api/v1/nodes/{node_key}/apply-settings', { params: { path: { node_key: node.key },
          header: { 'Idempotency-Key': keys.apply, 'If-Match': `"${saved.desired_revision}"` } } }));
      }
      await onsaved(saved);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const multiline = (field: FieldKey) => field === 'notes' || field === 'awg_parameters';
</script>

<section class="block" transition:unfold>
  <h2>{t('nodes.settings')}</h2>
  {#if failure}<p class="error">{failure}</p>{/if}
  {#if form}
    <div class="tabs" role="tablist" aria-label={t('nodes.settings')}>
      {#each tabs as item (item)}
        <button role="tab" aria-selected={tab === item} class:active={tab === item} onclick={() => (tab = item)}>
          {t('nodes.tab_' + item)}</button>
      {/each}
    </div>
    <form class="fields" onsubmit={(event) => { event.preventDefault(); save(review !== null); }}>
      {#if tab === 'protocols'}
        <fieldset>
          <legend>{t('nodes.protocols')}</legend>
          {#each ['awg', 'xray'] as protocol (protocol)}
            <label class="check"><input type="checkbox" checked={form.protocols.includes(protocol)}
              onchange={() => toggle(form!, protocol, false)} /> {protocol === 'awg' ? 'AmneziaWG' : 'VLESS'}</label>
          {/each}
        </fieldset>
        {#if form.protocols.includes('xray')}
          <fieldset>
            <legend>{t('nodes.transports')}</legend>
            {#each ['tcp', 'xhttp'] as transport (transport)}
              <label class="check"><input type="checkbox" checked={form.transports.includes(transport)}
                onchange={() => toggle(form!, transport, true)} /> {transport.toUpperCase()}</label>
            {/each}
          </fieldset>
        {/if}
        <p class="muted small">{t('nodes.protocols_tab_hint')}</p>
      {:else}
        {#if tab === 'awg'}<p class="muted small">{t('nodes.awg_hint')}</p>{/if}
        {#each fieldsOf(form, tab) as field (field)}
          <label>{t('nodes.field_' + field)}
            {#if CHOICES[field]}
              <select bind:value={form.values[field]}>
                {#each CHOICES[field] ?? [] as choice (choice)}<option value={choice}>{field === 'xray_fingerprint' ? choice
                  : t('nodes.choice_' + field + '_' + choice)}</option>{/each}
              </select>
            {:else if multiline(field)}
              <textarea rows={field === 'awg_parameters' ? 10 : 3} class:mono={field === 'awg_parameters'} bind:value={form.values[field]}></textarea>
            {:else}
              <input bind:value={form.values[field]} inputmode={field.endsWith('_port') ? 'numeric' : 'text'} />
            {/if}
            {#if errors[field]}<span class="error small">{t('nodes.error_' + errors[field], { field: t('nodes.field_' + field) })}</span>{/if}
          </label>
        {/each}
      {/if}
      <div class="row">
        <button class="primary" class:armed={review !== null} disabled={busy || !dirty(form)}>{t('people.save')}</button>
        <button type="button" onclick={oncancel}>{t('common.cancel')}</button>
      </div>
      {#if review}
        <Confirm text={t('nodes.region_confirm', { count: review.affected })} action={t('people.save')} cancel={() => (review = null)} />
      {/if}
    </form>
  {/if}
</section>

<style>
  .fields {
    display: grid;
    gap: 0.8rem;
    max-width: 34rem;
    margin-top: 1rem;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.5rem;
  }
  legend {
    width: 100%;
    margin-bottom: 0.25rem;
  }
  .small {
    font-size: 13px;
  }
  .mono {
    font-family: var(--mono, monospace);
    font-size: 13px;
  }
</style>
