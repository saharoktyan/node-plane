<!-- Operations on a server, or without one on every server and the
     controller, that wait for recovery, as npw lists them: what
     each is, why it stopped, what to check, and the actions the controller
     allows for it now, each asked again with what it will do. -->
<script lang="ts">
  import { confirmed } from '$lib/admin/confirmation.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import { explanation, recoveryPath } from './tools';
  import { catalogs, i18n, t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { toast } from '$lib/ui/toast.svelte';

  type Item = { id: string; kind: string; status: string; error_code: string; next_step: string; phase: string;
    actions: string[]; node_key: string };
  let { node }: { node?: string } = $props();
  let items = $state<Item[] | null>(null);
  let total = $state(0);
  let failure = $state('');
  let busy = $state(false);
  let armed = $state<{ id: string; action: string } | null>(null);

  export async function load() {
    try {
      const page = (await call(api.GET('/api/v1/system/recovery', { params: { query: node ? { node_key: node } : {} } }))) as unknown as
        { items: Item[]; total: number };
      items = node ? page.items.filter((item) => item.node_key === node) : page.items;
      total = page.total;
      failure = '';
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  const has = (key: string) => `nodes.${key}` in (catalogs[i18n.language] ?? {});
  const cause = (code: string) => (has('cause_' + code) ? t('nodes.cause_' + code) : errorText(new ApiError(0, code, '')));

  async function act(item: Item, action: string) {
    const path = recoveryPath(item.kind, item.id, action);
    if (!path) return;
    busy = true;
    failure = '';
    try {
      const body = action === 'force' ? { confirm_operation_id: item.id, acknowledge_unknown_outcome: true } : undefined;
      await confirmed(() => call(api.POST(path as never, { body } as never)));
      armed = null;
      toast(t('nodes.recovery_sent'));
      await load();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

{#if failure}<p class="error">{failure}</p>{/if}
{#if items && !items.length}<p class="muted small">{t('nodes.recovery_none')}</p>{/if}
{#each items ?? [] as item (item.kind + item.id)}
  <div class="item">
    <p><strong>{t('nodes.recovery_kind_' + item.kind)}</strong> <span class="muted">· {t('nodes.op_' + item.status)}
      {#if item.phase}· {item.phase}{/if}{#if !node && item.node_key}· <a href={'/admin/nodes/' + item.node_key}>{item.node_key}</a>{/if}</span></p>
    {#if item.error_code}<p class="small">{cause(item.error_code)}</p>{/if}
    <p class="muted small">{t('nodes.recovery_hint_' + (has('recovery_hint_' + item.next_step) ? item.next_step : 'worker'))}</p>
    {#if item.actions.some((action) => recoveryPath(item.kind, item.id, action))}
      <div class="row">
        {#each item.actions.filter((action) => recoveryPath(item.kind, item.id, action)) as action (action)}
          {@const on = armed?.id === item.id && armed.action === action}
          <button class:armed={on} class:danger={on && ['force', 'cancel', 'abort'].includes(action)} disabled={busy}
            onclick={() => (on ? act(item, action) : (armed = { id: item.id, action }))}>{t('nodes.recovery_' + action)}</button>
        {/each}
        {#if armed?.id === item.id}
          <Confirm text={t('nodes.' + explanation(item.kind, armed.action, has))} action={t('nodes.recovery_' + armed.action)}
            cancel={() => (armed = null)} />
        {/if}
      </div>
    {/if}
    <p class="muted id">{item.id}</p>
  </div>
{/each}
{#if items && total > items.length}<p class="muted small">{t('nodes.recovery_more', { count: total - items.length })}</p>{/if}

<style>
  .small {
    font-size: 13px;
  }
  .item {
    padding: 0.6rem 0;
    border-bottom: 1px solid var(--line);
  }
  .item p {
    margin: 0 0 0.25rem;
  }
  .id {
    font-family: var(--mono);
    font-size: 12px;
  }
</style>
