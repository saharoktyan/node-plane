<!-- Which servers a profile reaches. Rules give every server, or a region's,
     with some protocols, including servers added later; a server is also
     given or taken by hand. The draft is saved at once, on the revision it
     was read on. A staff member's VPN reaches everything and is not edited. -->
<script lang="ts">
  import { unfold } from '$lib/ui/motion';
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import { PROTOCOLS, bulk, effective, has, inherited, rule, same, toggle,
    type AccessNode, type Policy, type Protocol } from './access';

  let { profile, permanent, onchange }: {
    profile: Schemas['ProfileOutput'];
    permanent: boolean;
    onchange: () => Promise<void>;
  } = $props();

  type Loaded = Policy & { revision: number; permanent: boolean };
  type Region = { id: string; title: string };
  let saved = $state<Loaded | null>(null);
  let draft = $state<Policy | null>(null);
  let nodes = $state<AccessNode[]>([]);
  let regions = $state<Region[]>([]);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);

  const label = (protocol: string) => (protocol === 'awg' ? 'AWG' : 'VLESS');

  async function all<T>(read: (cursor?: string) => Promise<{ items: T[]; next_cursor?: string | null }>) {
    const items: T[] = [];
    let cursor: string | undefined;
    do {
      const page = await read(cursor);
      items.push(...page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    return items;
  }

  async function load() {
    failure = '';
    try {
      const [policy, loadedNodes, loadedRegions] = await Promise.all([
        call(api.GET('/api/v1/profiles/{profile_id}/access-policy', { params: { path: { profile_id: profile.id } } })),
        all((cursor) => call(api.GET('/api/v1/nodes', { params: { query: { stage: 'node', limit: 100, cursor } } }))
          .then((page) => page as unknown as { items: AccessNode[]; next_cursor: string | null })),
        all((cursor) => call(api.GET('/api/v1/regions', { params: { query: { limit: 100, cursor } } }))
          .then((page) => page as unknown as { items: Region[]; next_cursor: string | null }))
      ]);
      saved = policy as unknown as Loaded;
      nodes = loadedNodes.sort((a, b) => (a.region ?? '').localeCompare(b.region ?? '') || a.title.localeCompare(b.title));
      regions = loadedRegions.sort((a, b) => a.title.localeCompare(b.title));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  // Read again when the profile changes elsewhere, but not under a draft.
  $effect(() => {
    void profile.desired_revision;
    if (!draft) load();
  });

  const current = $derived(draft ?? saved);
  const given = $derived(current ? effective(current, nodes) : new Set<string>());
  const ruled = $derived(current ? inherited(current, nodes) : new Set<string>());
  const reached = $derived(nodes.filter((node) => node.protocols.some((p) => has(given, node.key, p))));
  const changed = $derived(draft != null && saved != null && !same(draft, saved));
  const groups = $derived.by(() => {
    const result = new Map<string, AccessNode[]>();
    for (const node of nodes) {
      const region = node.region || t('profiles.no_region');
      result.set(region, [...(result.get(region) ?? []), node]);
    }
    return [...result];
  });
  const scopes = $derived([{ scope: 'all' as const, id: null, title: t('profiles.rule_all') },
    ...regions.map((region) => ({ scope: 'region' as const, id: region.id, title: region.title }))]);
  const ruleHas = (scope: 'all' | 'region', id: string | null, protocol: Protocol) =>
    current?.rules.some((item) => item.scope === scope && (item.region_id ?? null) === id && item.protocols.includes(protocol));

  function edit() {
    if (!saved) return;
    draft = { explicit_grants: [...saved.explicit_grants], rules: [...saved.rules], exclusions: [...saved.exclusions] };
    notice = '';
  }

  async function save() {
    if (!draft || !saved) return;
    busy = true;
    failure = notice = '';
    try {
      await call(api.PUT('/api/v1/profiles/{profile_id}/access-policy', { params: { path: { profile_id: profile.id },
        header: { 'Idempotency-Key': key(), 'If-Match': `"${saved.revision}"` } }, body: draft }));
      draft = null;
      notice = t('profiles.access_saved');
      await onchange();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

<section class="block">
  <div class="block-head">
    <h2>{t('profiles.access')} {#if saved}<small>{t('profiles.access_count', { count: reached.length })}</small>{/if}</h2>
    {#if saved && !draft && !permanent && account.may('grants.manage')}
      <button onclick={edit}>{t('people.change')}</button>
    {/if}
  </div>
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}

  {#if permanent}
    <p class="muted small">{t('profiles.access_staff')}</p>
  {:else if saved && !draft}
    {#if saved.rules.length}
      <p class="small">{t('profiles.rules')}: {saved.rules.map((item) => (item.scope === 'all' ? t('profiles.rule_all')
        : regions.find((region) => region.id === item.region_id)?.title ?? item.region_id) + ' — '
        + item.protocols.map(label).join(' · ')).join('; ')}</p>
    {/if}
    {#if reached.length}
      <ul class="chips">
        {#each reached as node (node.key)}
          <li>{node.flag} {node.title} <span class="muted">{PROTOCOLS.filter((p) => has(given, node.key, p)).map(label).join(' · ')}</span></li>
        {/each}
      </ul>
    {:else}<p class="muted">{t('profiles.access_none')}</p>{/if}
  {:else if draft}
    <div transition:unfold>
    <h3>{t('profiles.rules')}</h3>
    <p class="muted small">{t('profiles.rules_hint')}</p>
    <div class="table"><table>
      <tbody>
        {#each scopes as scope (scope.id ?? 'all')}
          <tr><td>{scope.title}</td>
            {#each PROTOCOLS as protocol (protocol)}
              <td class="toggle"><label class="check"><input type="checkbox" checked={ruleHas(scope.scope, scope.id, protocol)}
                onchange={(event) => (draft = rule(draft!, scope.scope, scope.id, protocol, event.currentTarget.checked))} />
                {label(protocol)}</label></td>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table></div>

    <h3>{t('profiles.servers_list')}</h3>
    <p class="muted small">{t('profiles.servers_hint')}</p>
    {#each groups as [region, items] (region)}
      <div class="region">
        <strong>{region}</strong>
        <span class="row">
          <button class="bare" onclick={() => (draft = bulk(draft!, nodes, items, true))}>{t('profiles.give_all')}</button>
          <button class="bare" onclick={() => (draft = bulk(draft!, nodes, items, false))}>{t('profiles.take_all')}</button>
        </span>
      </div>
      <div class="table"><table>
        <tbody>
          {#each items as node (node.key)}
            <tr><td>{node.flag} {node.title}
              {#if node.enabled === false}<span class="muted small">· {t('profiles.node_off')}</span>{/if}</td>
              {#each PROTOCOLS as protocol (protocol)}
                <td class="toggle">
                  {#if node.protocols.includes(protocol)}
                    <label class="check"><input type="checkbox" checked={has(given, node.key, protocol)}
                      onchange={(event) => (draft = toggle(draft!, nodes, node.key, protocol, event.currentTarget.checked))} />
                      {label(protocol)}{#if has(ruled, node.key, protocol)}<span class="muted small"> · {t('profiles.by_rule')}</span>{/if}</label>
                  {:else}<span class="muted">—</span>{/if}
                </td>
              {/each}
            </tr>
          {/each}
        </tbody>
      </table></div>
    {/each}
    <div class="row actions">
      <button class="primary" disabled={busy || !changed} onclick={save}>{t('people.save')}</button>
      <button onclick={() => (draft = null)}>{t('common.cancel')}</button>
    </div>
    </div>
  {:else if !failure}<p class="muted">{t('common.loading')}</p>{/if}
</section>

<style>
  .small {
    font-size: 13px;
  }
  h2 small {
    font-weight: 400;
    font-size: 0.9rem;
    margin-left: 0.5rem;
    color: var(--muted);
  }
  h3 {
    margin: 1.25rem 0 0.25rem;
    font-size: 1rem;
  }
  .chips {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1.25rem;
  }
  .chips li {
    font-size: 14px;
  }
  table {
    table-layout: fixed;
  }
  .toggle {
    width: 11rem;
    white-space: nowrap;
  }
  @media (max-width: 640px) {
    .toggle {
      width: 6.5rem;
    }
    .toggle .muted {
      display: none;
    }
  }
  .region {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin: 1rem 0 0.25rem;
  }
  .actions {
    margin-top: 1rem;
  }
</style>
