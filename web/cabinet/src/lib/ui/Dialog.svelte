<!-- A window over the page: the browser's own dialog, so Escape, focus and
     the backdrop work as everywhere; a click on the backdrop closes it. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { t } from '$lib/i18n/index.svelte';

  let { open = $bindable(false), title, children }: { open: boolean; title: string; children: Snippet } = $props();
  let dialog: HTMLDialogElement;

  $effect(() => {
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  });
</script>

<dialog bind:this={dialog} onclose={() => (open = false)}
  onclick={(event) => event.target === dialog && dialog.close()}>
  <div class="inner">
    <div class="head">
      <h2>{title}</h2>
      <button class="close" onclick={() => dialog.close()} aria-label={t('vpn.close')}>×</button>
    </div>
    {@render children()}
  </div>
</dialog>

<style>
  dialog {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    padding: 0;
    width: min(32rem, calc(100vw - 2rem));
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 0.4);
  }
  .inner {
    padding: 1.1rem 1.25rem 1.25rem;
  }
  .head {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 1rem;
  }
  h2 {
    margin: 0 0 0.75rem;
  }
  .close {
    border: none;
    background: none;
    font-size: 1.4rem;
    line-height: 1;
    padding: 0 0.25rem;
    color: var(--muted);
  }
</style>
