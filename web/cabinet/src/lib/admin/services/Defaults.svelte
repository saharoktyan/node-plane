<!-- What a new server starts with, as Workstation sets it: its protocols
     and VLESS transports, IPv6 outbound and, under advanced, the AWG and
     VLESS settings. A server already made keeps its own. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import { CHOICES, check, type FieldKey } from '$lib/admin/node/editor';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';

  type Defaults = { revision: number; protocols: string[]; xray_transports: string[]; settings: Record<string, unknown> };
  // The settings a default may hold, with the value a server gets without one.
  const SETTINGS: [FieldKey, string][] = [['awg_i1_preset', 'quic'], ['awg_port_mode', 'auto'], ['awg_port', '443'],
    ['awg_interface', 'wg0'], ['xray_sni', 'www.cloudflare.com'], ['xray_fingerprint', 'chrome'],
    ['xray_tcp_port', '443'], ['xray_xhttp_port', '8443'], ['xray_xhttp_path', '/assets']];

  let source = $state<Defaults | null>(null);
  let form = $state<{ protocols: string[]; transports: string[]; ipv6: string; values: Record<string, string> } | null>(null);
  let advanced = $state(false);
  let failure = $state('');
  let busy = $state(false);
  const editable = $derived(account.may('settings.manage'));

  function start(defaults: Defaults) {
    source = defaults;
    const values = Object.fromEntries(SETTINGS.map(([field, fallback]) =>
      [field, defaults.settings[field] == null ? fallback : String(defaults.settings[field])]));
    form = { protocols: [...defaults.protocols], transports: [...defaults.xray_transports],
      ipv6: defaults.settings.ipv6_outbound === 'off' ? 'off' : 'on', values };
  }

  async function load() {
    try {
      start((await call(api.GET('/api/v1/system/installation-defaults'))) as unknown as Defaults);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  const toggle = (list: string[], item: string) => (list.includes(item) ? list.filter((value) => value !== item) : [...list, item]);
  // Only the fields of the protocols chosen, and the AWG port only when it is set by hand.
  const shown = $derived(form ? SETTINGS.map(([field]) => field).filter((field) =>
    (field.startsWith('awg') ? form!.protocols.includes('awg') : form!.protocols.includes('xray'))
    && (field !== 'awg_port' || form!.values.awg_port_mode === 'manual')) : []);
  const errors = $derived(Object.fromEntries(shown.map((field) => [field, check(field, form!.values[field])])));

  async function save() {
    if (!form || !source) return;
    failure = '';
    const wrong = shown.find((field) => errors[field]);
    if (wrong) {
      advanced = true;
      failure = t('nodes.error_' + errors[wrong], { field: t('nodes.field_' + wrong) });
      return;
    }
    if (form.protocols.includes('xray') && !form.transports.length) {
      failure = t('nodes.error_transport');
      return;
    }
    const settings: Record<string, unknown> = { ipv6_outbound: form.ipv6 };
    for (const [field] of SETTINGS) {
      if (field === 'awg_port' && form.values.awg_port_mode !== 'manual') continue;
      const value = form.values[field].trim();
      settings[field] = field.endsWith('_port') ? Number(value) : value;
    }
    busy = true;
    try {
      const saved = await call(api.PUT('/api/v1/system/installation-defaults', { headers: { 'If-Match': `"${source.revision}"` },
        body: { protocols: form.protocols as ('awg' | 'xray')[], xray_transports: form.protocols.includes('xray')
          ? form.transports as ('tcp' | 'xhttp')[] : [], settings } }));
      start(saved as unknown as Defaults);
      toast(t('vpnadmin.defaults_saved'));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

<p class="muted small">{t('vpnadmin.defaults_hint')}</p>
{#if failure}<p class="error">{failure}</p>{/if}
{#if form}
  <form class="fields" onsubmit={(event) => { event.preventDefault(); save(); }}>
    <fieldset disabled={!editable}>
      <legend>{t('nodes.protocols')}</legend>
      {#each ['awg', 'xray'] as protocol (protocol)}
        <label class="check"><input type="checkbox" checked={form.protocols.includes(protocol)}
          onchange={() => (form!.protocols = toggle(form!.protocols, protocol))} /> {protocol === 'awg' ? 'AmneziaWG' : 'VLESS'}</label>
      {/each}
    </fieldset>
    {#if form.protocols.includes('xray')}
      <fieldset disabled={!editable}>
        <legend>{t('nodes.transports')}</legend>
        {#each ['tcp', 'xhttp'] as transport (transport)}
          <label class="check"><input type="checkbox" checked={form.transports.includes(transport)}
            onchange={() => (form!.transports = toggle(form!.transports, transport))} /> {transport.toUpperCase()}</label>
        {/each}
      </fieldset>
    {/if}
    <label>{t('nodes.field_ipv6_outbound')}
      <select bind:value={form.ipv6} disabled={!editable}>
        {#each CHOICES.ipv6_outbound ?? [] as choice (choice)}<option value={choice}>{t('nodes.choice_ipv6_outbound_' + choice)}</option>{/each}
      </select>
    </label>
    {#if shown.length}
      <button type="button" class="more" aria-expanded={advanced} onclick={() => (advanced = !advanced)}>
        {t(advanced ? 'vpnadmin.hide_advanced' : 'vpnadmin.advanced')}</button>
    {/if}
    {#if advanced}
      {#each shown as field (field)}
        <label>{t('nodes.field_' + field)}
          {#if CHOICES[field]}
            <select bind:value={form.values[field]} disabled={!editable}>
              {#each CHOICES[field] ?? [] as choice (choice)}<option value={choice}>{field === 'xray_fingerprint' ? choice
                : t('nodes.choice_' + field + '_' + choice)}</option>{/each}
            </select>
          {:else}
            <input bind:value={form.values[field]} disabled={!editable} inputmode={field.endsWith('_port') ? 'numeric' : 'text'} />
          {/if}
          {#if errors[field]}<span class="error small">{t('nodes.error_' + errors[field], { field: t('nodes.field_' + field) })}</span>{/if}
        </label>
      {/each}
    {/if}
    {#if editable}
      <div class="row"><button class="primary" disabled={busy}>{t('people.save')}</button>
        <button type="button" disabled={busy} onclick={() => source && start(source)}>{t('vpnadmin.reset_draft')}</button></div>
    {/if}
  </form>
{/if}

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
  .more {
    justify-self: start;
  }
  .small {
    font-size: 13px;
  }
</style>
