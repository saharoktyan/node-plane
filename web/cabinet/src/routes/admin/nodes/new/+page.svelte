<!-- A new server, as npw makes one: where it is (a ready location or one of
     its own), how the controller reaches it and which VPN protocols it
     carries. It is only a registry entry: its card then installs the agent
     and the protocols. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { api, call, ApiError } from '$lib/api/client';
  import { validHost, validTarget } from '$lib/admin/node/editor';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key as commandKey } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';

  type Template = { code: string; title: string; region: string; flag: string; draft: { key: string; title: string } };
  type Options = { local_available: boolean; templates: Template[];
    defaults: { protocols: string[]; xray_transports: string[]; settings: Record<string, unknown> } };

  const REGIONS = ['Europe', 'Asia', 'North America', 'South America', 'Africa', 'Oceania'];
  let options = $state<Options | null>(null);
  let failure = $state('');
  let busy = $state(false);
  let form = $state({ transport: 'remote', template: '', key: '', title: '', region: 'Europe', otherRegion: '', flag: '',
    target: '', host: '', protocols: ['awg', 'xray'] as string[] });
  let available = $state<{ key: string; reason: string | null; suggestion: string | null } | null>(null);
  const command = commandKey();

  call(api.GET('/api/v1/nodes/creation-options')).then((value) => {
    options = value as unknown as Options;
    form.protocols = [...options.defaults.protocols];
    form.template = options.templates[0]?.code ?? '';
    pick();
  }).catch((error) => (failure = error instanceof ApiError ? errorText(error) : t('common.error')));

  const template = $derived(options?.templates.find((item) => item.code === form.template) ?? null);

  // A ready location fills the code, name, region and flag.
  function pick() {
    if (!template) return;
    Object.assign(form, { key: template.draft.key, title: template.draft.title, flag: template.flag,
      region: REGIONS.includes(template.region) ? template.region : '', otherRegion: REGIONS.includes(template.region) ? '' : template.region });
  }

  let timer: ReturnType<typeof setTimeout> | undefined;
  function keyed() {
    available = null;
    clearTimeout(timer);
    const value = form.key.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value)) return;
    timer = setTimeout(async () => {
      const result = (await call(api.GET('/api/v1/nodes/key-availability', { params: { query: { key: value } } }))
        .catch(() => null)) as { reason?: string | null; suggestion?: string | null } | null;
      if (result && form.key.trim() === value) available = { key: value, reason: result.reason ?? null, suggestion: result.suggestion ?? null };
    }, 300);
  }

  const region = $derived(form.region || form.otherRegion.trim());
  const errors = $derived({
    key: template || /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(form.key.trim()) ? null : 'nodes.new_key_invalid',
    title: form.title.trim() && form.title.length <= 128 ? null : 'nodes.new_title_invalid',
    region: region && region.length <= 128 ? null : 'nodes.new_region_invalid',
    flag: [...form.flag].length <= 16 ? null : 'nodes.new_flag_invalid',
    // Optional: without it the agent installs from a one-time command.
    target: form.transport !== 'remote' || !form.target.trim() || validTarget(form.target.trim().replace(/:22$/, '')) ? null : 'nodes.new_target_invalid',
    host: validHost(form.host.trim()) || (form.transport === 'local' && form.host.trim() === '.') ? null : 'nodes.new_host_invalid'
  });
  const ready = $derived(options && Object.values(errors).every((value) => !value) && !(available?.reason && !template));
  let touched = $state<Record<string, boolean>>({});

  async function create(event: SubmitEvent) {
    event.preventDefault();
    touched = { key: true, title: true, region: true, flag: true, target: true, host: true };
    if (!ready || !options) return;
    busy = true;
    failure = '';
    const xray = form.protocols.includes('xray');
    const body: Record<string, unknown> = { key: form.key.trim(), title: form.title.trim(), region, flag: form.flag.trim(),
      transport: form.transport, ssh_target: form.transport === 'remote' && form.target.trim() ? form.target.trim().replace(/:22$/, '') : null,
      protocols: ['awg', 'xray'].filter((item) => form.protocols.includes(item)),
      xray_transports: xray ? (options.defaults.xray_transports.length ? options.defaults.xray_transports : ['tcp', 'xhttp']) : [],
      settings: { ...options.defaults.settings, public_host: form.host.trim() } };
    if (template) body.template = template.code;
    try {
      const created = (await call(api.POST('/api/v1/nodes', { params: { header: { 'Idempotency-Key': command } },
        body: body as never }))) as unknown as { key: string };
      await goto('/admin/nodes/' + created.key);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const toggle = (protocol: string) => (form.protocols = form.protocols.includes(protocol)
    ? form.protocols.filter((item) => item !== protocol) : [...form.protocols, protocol]);
</script>

<nav class="crumbs"><a href="/admin/nodes">{t('admin.nodes')}</a></nav>
<div class="page-head"><h1>{t('nodes.new')}</h1></div>
<p class="muted small lead">{t('nodes.new_lead')}</p>
{#if failure}<p class="error">{failure}</p>{/if}

{#if options}
  <form class="block fields" onsubmit={create}>
    {#if options.local_available}
      <fieldset>
        <legend>{t('nodes.new_type')}</legend>
        <label class="check"><input type="radio" name="transport" value="remote" bind:group={form.transport} /> {t('nodes.new_remote')}</label>
        <label class="check"><input type="radio" name="transport" value="local" bind:group={form.transport} /> {t('nodes.new_local')}</label>
      </fieldset>
    {/if}

    <label>{t('nodes.new_location')}
      <select bind:value={form.template} onchange={pick}>
        {#each options.templates as item (item.code)}<option value={item.code}>{item.flag} {item.title} · {item.region}</option>{/each}
        <option value="">{t('nodes.new_custom')}</option>
      </select>
    </label>

    {#if template}
      <p class="muted small hint">{t('nodes.new_template_hint', { key: form.key })}</p>
    {:else}
      <div transition:unfold class="fields">
        <label>{t('nodes.code')}
          <input bind:value={form.key} oninput={keyed} onblur={() => (touched.key = true)} maxlength="64" autocomplete="off" />
          {#if touched.key && errors.key}<span class="error small">{t(errors.key)}</span>
          {:else if available?.reason && available.key === form.key.trim()}
            <span class="error small">{t('nodes.new_' + available.reason)}
              {#if available.suggestion}<button type="button" class="bare link" onclick={() => { form.key = available!.suggestion!; keyed(); }}>
                {t('nodes.new_use', { key: available.suggestion })}</button>{/if}</span>
          {/if}
        </label>
        <label>{t('nodes.field_title')}
          <input bind:value={form.title} onblur={() => (touched.title = true)} maxlength="128" />
          {#if touched.title && errors.title}<span class="error small">{t(errors.title)}</span>{/if}
        </label>
        <label>{t('nodes.field_region')}
          <select bind:value={form.region}>
            {#each REGIONS as item (item)}<option value={item}>{item}</option>{/each}
            <option value="">{t('nodes.region_other')}</option>
          </select>
        </label>
        {#if !form.region}
          <label transition:unfold>{t('nodes.new_region_name')}
            <input bind:value={form.otherRegion} onblur={() => (touched.region = true)} maxlength="128" />
            {#if touched.region && errors.region}<span class="error small">{t(errors.region)}</span>{/if}
          </label>
        {/if}
        <label>{t('nodes.new_flag')}
          <input bind:value={form.flag} class="flag" />
          {#if errors.flag}<span class="error small">{t(errors.flag)}</span>{/if}
        </label>
      </div>
    {/if}

    {#if form.transport === 'remote'}
      <label>{t('nodes.field_ssh_target')}
        <input bind:value={form.target} onblur={() => (touched.target = true)} placeholder="root@203.0.113.10" autocomplete="off" />
        <span class="muted small">{t('nodes.new_target_hint')}</span>
        {#if touched.target && errors.target}<span class="error small">{t(errors.target)}</span>{/if}
      </label>
    {/if}
    <label>{t('nodes.new_host')}
      <input bind:value={form.host} onblur={() => (touched.host = true)} placeholder="vpn.example.net" autocomplete="off" />
      <span class="muted small">{t(form.transport === 'local' ? 'nodes.new_host_local_hint' : 'nodes.new_host_hint')}</span>
      {#if touched.host && errors.host}<span class="error small">{t(errors.host)}</span>{/if}
    </label>

    <fieldset>
      <legend>{t('nodes.protocols')}</legend>
      {#each ['awg', 'xray'] as protocol (protocol)}
        <label class="check"><input type="checkbox" checked={form.protocols.includes(protocol)} onchange={() => toggle(protocol)} />
          {protocol === 'awg' ? 'AmneziaWG' : 'VLESS'}</label>
      {/each}
      {#if !form.protocols.length}<p class="muted small">{t('nodes.new_agent_only')}</p>{/if}
    </fieldset>

    <div class="row">
      <button class="primary" disabled={busy}>{t('nodes.new_create')}</button>
      <a class="button" href="/admin/nodes">{t('common.cancel')}</a>
    </div>
  </form>
{/if}

<style>
  .lead {
    margin-top: -0.5rem;
    max-width: 44rem;
  }
  .small {
    font-size: 13px;
  }
  .fields {
    display: grid;
    gap: 0.9rem;
    max-width: 32rem;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1.5rem;
  }
  legend {
    width: 100%;
    margin-bottom: 0.25rem;
  }
  fieldset p {
    flex-basis: 100%;
    margin: 0;
  }
  .hint {
    margin: -0.4rem 0 0;
  }
  .flag {
    width: 6rem;
  }
  .link {
    margin-left: 0.5rem;
    text-decoration: underline;
  }
</style>
