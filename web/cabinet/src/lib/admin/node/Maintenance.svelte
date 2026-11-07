<!-- A server's maintenance, as in npw and the bot: diagnostics with the
     recovery of its operations, actions on its VPN, and its removal. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { installed, type Node, type Services } from '$lib/admin/nodes';
  import Actions from './Actions.svelte';
  import Diagnostics from './Diagnostics.svelte';
  import Removal from './Removal.svelte';
  import { t } from '$lib/i18n/index.svelte';

  let { node, services, busy, onchange, onclose }: {
    node: Node; services: Services | null; busy: boolean; onchange: () => Promise<void>; onclose: () => void;
  } = $props();

  const tabs = $derived([
    ...(installed(node) && account.may('maintenance.manage') ? ['diagnostics' as const] : []),
    ...(installed(node) && node.protocols.length && node.applied_revision > 0 ? ['actions' as const] : []),
    ...(account.may('maintenance.manage') ? ['removal' as const] : [])
  ]);
  let chosen = $state<'diagnostics' | 'actions' | 'removal' | null>(null);
  const tab = $derived(chosen && tabs.includes(chosen) ? chosen : tabs[0]);
</script>

<section class="block">
  <div class="block-head"><h2>{t('nodes.maintenance')}</h2>
    <button onclick={onclose}>{t('common.close')}</button></div>
  {#if tabs.length > 1}
    <div class="tabs" role="tablist" aria-label={t('nodes.maintenance')}>
      {#each tabs as name (name)}
        <button role="tab" aria-selected={tab === name} class:active={tab === name} onclick={() => (chosen = name)}>
          {t('nodes.maintenance_' + name)}</button>
      {/each}
    </div>
  {/if}
  {#if tab === 'diagnostics'}
    <Diagnostics node={node.key} />
  {:else if tab === 'actions'}
    <Actions {node} {services} {busy} {onchange} />
  {:else if tab === 'removal'}
    <Removal {node} />
  {/if}
</section>
