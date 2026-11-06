<!-- Connecting: a subscription (one link to every server; not yet made) or a
     server chosen by hand, as in the bot — the device, then a server and what
     to connect with there: VLESS over each transport the server offers, or
     AmneziaWG. The servers are searched and paged; a configuration opens
     under its server's row. -->
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
  const SIZE = 12;

  let tab = $state<'subscription' | 'servers'>('subscription');

  // Servers in service now and their transports; refused, the list still shows the servers.
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

  // The ways to connect, in the order of the list's columns.
  const SLOTS: Choice[] = [{ node: '', protocol: 'xray', transport: 'xhttp' }, { node: '', protocol: 'xray', transport: 'tcp' },
    { node: '', protocol: 'awg', transport: 'vpn' }];

  /** What a server offers; while its transports are not known, what the profile has there. */
  function choices(node: string): Choice[] {
    const result: Choice[] = [];
    const known = available.get(node);
    const protocols = known ?? (nodes.find((one) => one.key === node)?.protocols ?? [])
      .map((kind) => ({ kind, transports: kind === 'xray' ? ['xhttp', 'tcp'] : ['vpn'] }));
    for (const protocol of protocols) {
      if (protocol.kind === 'xray')
        for (const transport of ['xhttp', 'tcp'] as const)
          if (protocol.transports.includes(transport)) result.push({ node, protocol: 'xray', transport });
      if (protocol.kind === 'awg') result.push({ node, protocol: 'awg', transport: 'vpn' });
    }
    return result;
  }
  const label = (choice: Choice) => choice.protocol === 'awg' ? t('vpn.protocol_awg') : t('vpn.transport_' + choice.transport);
  const id = (choice: Choice) => `${choice.node}/${choice.protocol}/${choice.transport}`;
  const down = (node: string) => available.size > 0 && !available.has(node);

  // A flag's two letters, so «de» finds Germany.
  const country = (flag: string) => [...flag].map((char) => String.fromCharCode((char.codePointAt(0) ?? 0) - 0x1f1e6 + 65))
    .filter((letter) => /[A-Z]/.test(letter)).join('');

  let query = $state('');
  let page = $state(0);
  const found = $derived.by(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    return nodes.filter((node) => {
      const offered = down(node.key) ? [] : choices(node.key).map(label);
      const text = [country(node.flag), node.title, node.region, node.key, ...offered,
        ...node.protocols.map((protocol) => t('vpn.protocol_' + protocol)), node.protocols.includes('awg') ? 'awg' : '']
        .join(' ').toLowerCase();
      return words.every((word) => text.includes(word));
    });
  });
  const pages = $derived(Math.max(1, Math.ceil(found.length / SIZE)));
  const current = $derived(Math.min(page, pages - 1));
  const shown = $derived(found.slice(current * SIZE, current * SIZE + SIZE));
  function go(to: number) {
    page = Math.max(0, Math.min(pages - 1, to));
  }

  // The choice being issued; another press or leaving drops its answer.
  let pending = $state<string | null>(null);
  let notice = $state<{ node: string; text: string } | null>(null);
  let opened = $state<{ choice: string; issuance: Schemas['ConfigIssuanceOutput']; artifact: Schemas['ConfigArtifactOutput'] } | null>(null);

  async function issue(choice: Choice) {
    const mine = id(choice);
    if (opened?.choice === mine) {
      opened = null;
      return;
    }
    pending = mine;
    notice = null;
    opened = null;
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
        notice = { node: choice.node, text: t('vpn.config_failed') };
        return;
      }
      const artifact = await call(api.GET('/api/v1/config-issuances/{issuance_id}/artifact', {
        params: { path: { issuance_id: issuance.id } } }));
      if (pending === mine) opened = { choice: mine, issuance, artifact };
    } catch (error) {
      if (pending === mine) notice = { node: choice.node, text: error instanceof ApiError ? errorText(error) : t('common.error') };
    } finally {
      if (pending === mine) pending = null;
    }
  }

  function search(value: string) {
    query = value;
    page = 0;
  }
</script>

<section class="block">
  <div class="block-head tabbed">
    <h2>{t('vpn.connect')}</h2>
    <div class="tabs" role="tablist" aria-label={t('vpn.connect_ways')}>
      <button role="tab" aria-selected={tab === 'subscription'} onclick={() => (tab = 'subscription')}>
        {t('vpn.tab_subscription')}</button>
      <button role="tab" aria-selected={tab === 'servers'} onclick={() => (tab = 'servers')}>{t('vpn.tab_servers')}</button>
    </div>
  </div>

  <!-- Both panels stay in the page and fold, so the devices below slide rather than jump. -->
  <div class="fold" class:open={tab === 'subscription'} inert={tab !== 'subscription'}>
    <div>
      <p class="lead">{t('vpn.subscription_lead')}</p>
      <p class="muted">{t('vpn.subscription_later')}</p>
    </div>
  </div>

  <div class="fold" class:open={tab === 'servers'} inert={tab !== 'servers'}>
    <div>
      {#if !issuing}
        <p class="muted">{t('vpn.configs_closed')}</p>
      {:else if !devices.length}
        <p class="muted">{t('vpn.need_device')}</p>
      {/if}
      <div class="filters">
        <label class="search">
          {t('vpn.search')}
          <input type="search" value={query} placeholder={t('vpn.search_hint')}
            oninput={(event) => search(event.currentTarget.value)} />
        </label>
        {#if issuing && devices.length > 1}
          <label class="device">
            {t('vpn.for_device')}
            <select bind:value={device}>
              {#each devices as item (item.id)}<option value={item.id}>{item.display_name}</option>{/each}
            </select>
          </label>
        {/if}
      </div>
      {#if issuing && devices.length}<p class="muted hint">{t('vpn.connect_hint')}</p>{/if}

      {#if !nodes.length}
        <p class="muted">{t('vpn.no_servers')}</p>
      {:else if !found.length}
        <p class="muted">{t('vpn.nothing_found')}</p>
      {:else}
        <div class="table">
          <table>
            <thead><tr><th>{t('vpn.server')}</th><th>{t('vpn.region')}</th><th colspan={SLOTS.length}>{t('vpn.protocols')}</th></tr></thead>
            <tbody>
              {#each shown as node (node.key)}
                {@const offered = choices(node.key)}
                <tr class:off={down(node.key)}>
                  <td class="server">{node.flag} {node.title}</td>
                  <td class="muted region">{node.region || '—'}</td>
                  {#if down(node.key)}
                    <td colspan={SLOTS.length} class="unavailable">{t('vpn.unavailable_now')}</td>
                  {:else}
                    <!-- One column for each way to connect, so the same one stands under itself. -->
                    {#each SLOTS as slot (id(slot))}
                      {@const option = offered.find((one) => one.protocol === slot.protocol && one.transport === slot.transport)}
                      <td class="slot">
                        {#if !option}<span class="none">—</span>
                        {:else if issuing && devices.length}
                          <button class:chosen={opened?.choice === id(option) || pending === id(option)}
                            disabled={pending !== null} aria-pressed={opened?.choice === id(option)}
                            onclick={() => issue(option)}>{label(option)}</button>
                        {:else}{label(option)}{/if}
                      </td>
                    {/each}
                  {/if}
                </tr>
                {#if pending?.startsWith(node.key + '/')}
                  <tr class="under"><td colspan={SLOTS.length + 2} class="muted">{t('vpn.preparing')}</td></tr>
                {:else if notice?.node === node.key}
                  <tr class="under"><td colspan={SLOTS.length + 2} class="error">{notice.text}</td></tr>
                {:else if opened?.issuance.node_key === node.key}
                  <tr class="under">
                    <td colspan={SLOTS.length + 2}>
                      <Config issuance={opened.issuance} artifact={opened.artifact} onclose={() => (opened = null)} />
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
        <div class="pager">
          <span>{t('vpn.range', { from: current * SIZE + 1, to: current * SIZE + shown.length, total: found.length,
            page: current + 1, pages })}</span>
          {#if pages > 1}
            <span class="numbers">
              <button class="bare" disabled={current === 0} aria-label={t('vpn.previous')} onclick={() => go(current - 1)}>←</button>
              {#each { length: pages } as _, index (index)}
                <button class="bare" aria-label={t('vpn.page', { number: index + 1 })}
                  aria-current={index === current ? 'page' : undefined} onclick={() => go(index)}>{index + 1}</button>
              {/each}
              <button class="bare" disabled={current === pages - 1} aria-label={t('vpn.next')} onclick={() => go(current + 1)}>→</button>
            </span>
          {/if}
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .tabbed {
    align-items: flex-end;
    padding-bottom: 0;
  }
  .tabbed h2 {
    padding-bottom: 0.5rem;
  }
  /* A panel folds to nothing; a grid row eases between none and its height. */
  .fold {
    display: grid;
    grid-template-rows: 0fr;
    opacity: 0;
    transition: grid-template-rows 0.4s ease, opacity 0.25s ease;
  }
  .fold.open {
    grid-template-rows: 1fr;
    opacity: 1;
  }
  .fold > div {
    overflow: hidden;
    min-height: 0;
  }
  .lead {
    font-family: var(--serif);
    font-style: italic;
    font-size: 1.1rem;
    line-height: 1.5;
    max-width: 40rem;
    margin: 1.25rem 0 0.4rem;
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.75rem 1.75rem;
    padding-top: 1rem;
  }
  .filters label {
    margin-bottom: 0;
  }
  .search {
    flex: 1 1 18rem;
    max-width: 32rem;
  }
  .device {
    flex: 0 1 14rem;
  }
  .hint {
    margin: 0.75rem 0 0.25rem;
    font-size: 13px;
  }
  table {
    min-width: 34rem;
  }
  .server {
    font-weight: 600;
    white-space: nowrap;
  }
  tr.off td {
    color: var(--faint);
  }
  .unavailable {
    font-style: italic;
  }
  td.slot {
    white-space: nowrap;
    width: 1%;
    padding-right: 1.5rem;
  }
  td.slot:last-child {
    width: auto;
  }
  .none {
    color: var(--faint);
  }
  td.slot button {
    font-weight: 400;
    padding: 0.1rem 0;
  }
  td.slot button.chosen {
    font-weight: 600;
    text-decoration-color: var(--accent);
    text-decoration-thickness: 2px;
    color: var(--text);
  }
  tr.under > td {
    padding: 0;
  }
  tr.under > td.muted,
  tr.under > td.error {
    padding: 0.6rem 0 0.6rem 1rem;
    border-left: 2px solid var(--accent);
  }
  .pager {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1.25rem;
    padding: 0.75rem 0 0.25rem;
    font-size: 13px;
    color: var(--muted);
  }
  .numbers {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
  }
  .numbers button {
    min-width: 32px;
    font-weight: 400;
  }
  .numbers button[aria-current='page'] {
    color: var(--text);
    font-weight: 600;
    text-decoration: underline;
    text-decoration-color: var(--accent);
    text-decoration-thickness: 2px;
  }
  .numbers button:disabled {
    color: var(--line);
  }
  /* A phone: the server on a line of its own, its protocols under it in three
     even columns; the region gives way. */
  @media (max-width: 600px) {
    table {
      min-width: 0;
    }
    thead,
    td.region {
      display: none;
    }
    tbody tr:not(.under) {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
    }
    tbody tr:not(.under) > td {
      border-bottom: none;
      padding: 0.3rem 0.5rem 0.55rem 0;
      width: auto;
      font-size: 13px;
    }
    tbody tr:not(.under) > td.server {
      grid-column: 1 / -1;
      padding-top: 0.65rem;
      font-size: 14px;
    }
    tbody tr:not(.under) > td.unavailable {
      grid-column: 1 / -1;
    }
    tr.under {
      display: block;
    }
    tr.under > td {
      display: block;
    }
  }
</style>
