<!-- Where the news of finished actions shows: a corner over the page. -->
<script lang="ts">
  import { fly } from 'svelte/transition';
  import { toasts } from './toast.svelte';
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts.items as item (item.id)}
    <div class="toast" transition:fly={{ y: 12, duration: 250 }}>{item.text}</div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    right: 1.25rem;
    bottom: 1.25rem;
    z-index: 50;
    display: grid;
    gap: 0.5rem;
    justify-items: end;
    pointer-events: none;
  }
  .toast {
    max-width: min(24rem, calc(100vw - 2.5rem));
    padding: 0.6rem 0.9rem;
    border-left: 2px solid var(--accent);
    background: var(--panel);
    color: var(--text);
    font-size: 14px;
    box-shadow: 0 6px 24px rgb(0 0 0 / 0.25);
  }
</style>
