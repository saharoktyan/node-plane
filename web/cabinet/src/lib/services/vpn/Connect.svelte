<!-- Getting a configuration, as in the bot: the device it belongs to, then a
     server and what to connect with there — VLESS over each transport the
     server offers, or AmneziaWG. The configuration opens below the table. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import Config from './Config.svelte';

  let { profileId, nodes, devices, issuing }: {
    profileId: string;
    /** The servers the profile reaches; those not in service now are shown so. */
    nodes: Schemas['MemberProfileNode'][];
    devices: Schemas['DeviceOutput'][];
    /** Configurations are issued only while access is in force. */
    issuing: boolean;
  } = $props();

  type Choice = { node: string; protocol: 'xray' | 'awg'; transport: 'tcp' | 'xhttp' | 'vpn' };
  const DONE = ['succeeded', 'blocked', 'superseded', 'failed'];

  // Servers in service now and their transports; refused, the table still shows the servers.
  let available = $state<Map<string, Schemas['AvailableProtocol'][]>>(new Map());
  $effect(() => {
    call(api.GET('/api/v1/profiles/{profile_id}/nodes', { params: { path: { profile_id: profileId }, query: { limit: 100 } } }))
      .then((page) => (available = new Map(page.items.map((node) => [node.key, node.protocols]))))
      .catch(() => (available = new Map()));
  });

  let device = $state('');
  $effect(() => {
    if (!devices.some((item) => item.id === device)) device = devices[0]?.id ?? '';
  });

  function choices(node: string): Choice[] {
    const result: Choice[] = [];
    for (const protocol of available.get(node) ?? []) {
      if (protocol.kind === 'xray')
        for (const transport of ['xhttp', 'tcp'] as const)
          if (protocol.transports.includes(transport)) result.push({ node, protocol: 'xray', transport });
      if (protocol.kind === 'awg') result.push({ node, protocol: 'awg', transport: 'vpn' });
    }
    return result;
  }
  const label = (choice: Choice) => choice.protocol === 'awg' ? t('vpn.protocol_awg') : t('vpn.transport_' + choice.transport);

  const id = (choice: Choice) => `${choice.node}/${choice.protocol}/${choice.transport}`;
  // The choice being issued; another press or leaving drops its answer.
  let pending = $state<string | null>(null);
  let notice = $state('');
  let shown = $state<{ issuance: Schemas['ConfigIssuanceOutput']; artifact: Schemas['ConfigArtifactOutput'] } | null>(null);

  async function issue(choice: Choice) {
    const mine = id(choice);
    pending = mine;
    notice = '';
    shown = null;
    try {
      let issuance = await call(api.POST('/api/v1/profiles/{profile_id}/config-issuances', {
        params: { path: { profile_id: profileId }, query: { wait: true }, header: { 'Idempotency-Key': key() } },
        body: { node_key: choice.node, protocol: choice.protocol, transport: choice.transport, device_id: device }
      }));
      // AmneziaWG waits for the nodes to apply the device's key.
      for (let attempt = 0; !DONE.includes(issuance.status) && attempt < 40; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (pending !== mine) return;
        issuance = await call(api.GET('/api/v1/config-issuances/{issuance_id}', {
          params: { path: { issuance_id: issuance.id } } }));
      }
      if (pending !== mine) return;
      if (issuance.status !== 'succeeded') {
        notice = t('vpn.config_failed');
        return;
      }
      const artifact = await call(api.GET('/api/v1/config-issuances/{issuance_id}/artifact', {
        params: { path: { issuance_id: issuance.id } } }));
      if (pending === mine) shown = { issuance, artifact };
    } catch (error) {
      if (pending === mine) notice = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      if (pending === mine) pending = null;
    }
  }
</script>

<section class="block">
  <h2>{t('vpn.connect')}</h2>
  {#if !issuing}
    <p class="muted">{t('vpn.configs_closed')}</p>
  {:else if !devices.length}
    <p class="muted">{t('vpn.need_device')}</p>
  {:else}
    <p class="muted">{t('vpn.connect_hint')}</p>
    {#if devices.length > 1}
      <label class="device">
        {t('vpn.device')}
        <select bind:value={device}>
          {#each devices as item (item.id)}<option value={item.id}>{item.display_name}</option>{/each}
        </select>
      </label>
    {/if}
  {/if}
  {#if nodes.length}
    <table>
      <thead><tr><th>{t('vpn.server')}</th><th>{t('vpn.region')}</th><th>{t('vpn.protocols')}</th></tr></thead>
      <tbody>
        {#each nodes as node (node.key)}
          {@const options = issuing && devices.length ? choices(node.key) : []}
          <tr>
            <td>{node.flag} {node.title}</td>
            <td class="muted">{node.region || '—'}</td>
            <td>
              {#if options.length}
                <div class="row">
                  {#each options as option (id(option))}
                    <button class:primary={pending === id(option)} disabled={pending !== null}
                      onclick={() => issue(option)}>{label(option)}</button>
                  {/each}
                </div>
              {:else if issuing && devices.length && available.size && !available.has(node.key)}
                <span class="muted">{t('vpn.unavailable_now')}</span>
              {:else}
                {node.protocols.map((protocol) => t('vpn.protocol_' + protocol)).join(', ')}
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">{t('vpn.no_servers')}</p>
  {/if}
  {#if pending}<p class="muted">{t('vpn.preparing')}</p>{/if}
  {#if notice}<p class="error">{notice}</p>{/if}
  {#if shown}<Config issuance={shown.issuance} artifact={shown.artifact} onclose={() => (shown = null)} />{/if}
</section>

<style>
  .device {
    max-width: 20rem;
  }
</style>
