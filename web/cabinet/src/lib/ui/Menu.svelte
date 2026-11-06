<!-- A row's actions behind one «⋯» button: a list that opens under it and
     closes on a choice, a click elsewhere or Escape. A dangerous action
     comes last, set apart. -->
<script lang="ts">
  import { t } from '$lib/i18n/index.svelte';

  type Item = { label: string; danger?: boolean; select: () => void };
  let { items, label = t('common.actions') }: { items: Item[]; label?: string } = $props();
  let open = $state(false);
  let root: HTMLDivElement;

  function choose(item: Item) {
    open = false;
    item.select();
  }

  $effect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => !root.contains(event.target as Node) && (open = false);
    const escape = (event: KeyboardEvent) => event.key === 'Escape' && (open = false);
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  });
</script>

<div class="menu" bind:this={root}>
  <button class="bare trigger" aria-label={label} aria-haspopup="menu" aria-expanded={open}
    onclick={() => (open = !open)}>⋯</button>
  {#if open}
    <div class="list" role="menu">
      {#each items as item (item.label)}
        <button role="menuitem" class:danger={item.danger} onclick={() => choose(item)}>{item.label}</button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .menu {
    position: relative;
  }
  .trigger {
    font-size: 1.25rem;
    line-height: 1;
  }
  .trigger:hover,
  .trigger[aria-expanded='true'] {
    color: var(--text);
  }
  .list {
    position: absolute;
    right: 0;
    top: calc(100% + 0.25rem);
    z-index: 5;
    min-width: 13rem;
    display: grid;
    padding: 0.35rem 0;
    background: var(--bg);
    border: 1px solid var(--line);
    border-top: 2px solid var(--text);
    box-shadow: 0 10px 28px rgb(0 0 0 / 0.16);
  }
  .list button {
    text-decoration: none;
    font-weight: 400;
    text-align: left;
    padding: 0.5rem 0.9rem;
    white-space: nowrap;
  }
  .list button:hover {
    background: var(--panel);
  }
  .list button.danger {
    margin-top: 0.35rem;
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
  }
</style>
