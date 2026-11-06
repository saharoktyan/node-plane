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
      <button class="bare close" onclick={() => dialog.close()} aria-label={t('vpn.close')}>×</button>
    </div>
    {@render children()}
  </div>
</dialog>

<style>
  dialog {
    border: none;
    border-top: 2px solid var(--text);
    background: var(--bg);
    color: var(--text);
    padding: 0;
    width: min(34rem, calc(100vw - 2rem));
    box-shadow: 0 18px 48px rgb(0 0 0 / 0.22);
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 0.45);
  }
  .inner {
    padding: 1.25rem 1.5rem 1.5rem;
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
    font-size: 1.5rem;
    line-height: 1;
    margin: -0.4rem -0.6rem 0 0;
  }
  .close:hover {
    color: var(--text);
  }
</style>
