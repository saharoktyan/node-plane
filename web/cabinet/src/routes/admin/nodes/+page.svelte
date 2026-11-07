<!-- Servers by region: nodes with an agent and invitations still waiting
     for one, found by name, region or code, each opening its card. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { api, call, ApiError } from '$lib/api/client';
  import { stateKey, type Node } from '$lib/admin/nodes';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';

  let items = $state<Node[] | null>(null);
  let failure = $state('');
  let search = $state('');

  async function load() {
    const result: Node[] = [];
    let cursor: string | undefined;
    try {
      do {
        const page = await call(api.GET('/api/v1/nodes', { params: { query: { limit: 100, order: 'region', cursor } } }));
        result.push(...(page.items as unknown as Node[]));
        cursor = page.next_cursor ?? undefined;
      } while (cursor);
      items = result;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  const shown = $derived.by(() => {
    const query = search.trim().toLowerCase();
    return (items ?? []).filter((node) => !query
      || [node.title, node.region, node.key].some((value) => value.toLowerCase().includes(query)));
  });
  const regions = $derived.by(() => {
    const groups = new Map<string, Node[]>();
    for (const node of shown) groups.set(node.region || '—', [...(groups.get(node.region || '—') ?? []), node]);
    return [...groups];
  });
  const tone = (key: string) => (['applied_unverified', 'active'].includes(key) ? 'ok'
    : ['needs_attention', 'deletion_blocked'].includes(key) ? 'danger' : 'warn');
</script>

<div class="page-head">
  <h1>{t('admin.nodes')}</h1>
</div>
<div class="row search">
  <input type="search" placeholder={t('nodes.search')} aria-label={t('nodes.search')} bind:value={search} />
</div>
{#if failure}<p class="error">{failure}</p>{/if}
{#if items !== null && !items.length}
  <p class="muted">{t('admin.no_nodes')}</p>
{:else if items}
  <div class="table"><table>
    <colgroup><col /><col class="code-col" /><col class="protocols-col" /><col class="state-col" /></colgroup>
    <tbody>
      {#each regions as [region, nodes] (region)}
        <tr class="region"><th colspan="4">{region}</th></tr>
        {#each nodes as node (node.key)}
          {@const key = stateKey(node)}
          <tr class="link" onclick={() => goto('/admin/nodes/' + node.key)}>
            <td class="name"><a href={'/admin/nodes/' + node.key}>{node.flag} {node.title}</a></td>
            <td class="muted code">{node.key}</td>
            <td class="muted">{['awg', 'xray'].filter((p) => node.protocols.includes(p)).map((p) => (p === 'awg' ? 'AWG' : 'VLESS')).join(' · ') || '—'}</td>
            <td class="state"><span class="badge {tone(key)}">{t('nodes.state_' + key)}</span></td>
          </tr>
        {/each}
      {/each}
    </tbody>
  </table></div>
  {#if !shown.length}<p class="muted">{t('nodes.nothing_found')}</p>{/if}
{/if}

<style>
  .search {
    margin-bottom: 1rem;
    max-width: 24rem;
  }
  @media (min-width: 641px) {
    table {
      table-layout: fixed;
    }
    .code-col {
      width: 7rem;
    }
    .protocols-col {
      width: 9rem;
    }
    .state-col {
      width: 13rem;
    }
  }
  tr.region th {
    font-family: var(--serif);
    font-size: 1rem;
    text-transform: none;
    letter-spacing: 0;
    color: var(--text);
    padding-top: 1.25rem;
  }
  /* A phone keeps the name and the state; the rest is on the card. */
  @media (max-width: 640px) {
    col.code-col,
    col.protocols-col,
    td.code,
    td.code + td {
      display: none;
    }
  }

  .name {
    font-weight: 600;
  }
  .name a {
    color: inherit;
  }
  .code {
    font-family: var(--mono, monospace);
    font-size: 13px;
  }
  .state {
    text-align: right;
  }
  tr.link {
    cursor: pointer;
  }
</style>
