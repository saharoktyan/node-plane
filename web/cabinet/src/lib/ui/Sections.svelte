<!-- A page's sections as one heading of words, the chosen one underlined
     in the accent; something at its right, such as an action, is optional. -->
<script lang="ts" generics="Name extends string">
  import type { Snippet } from 'svelte';

  let { names, current = $bindable(), label, title, aside }: {
    names: Name[]; current: Name; label: string; title: (name: Name) => string; aside?: Snippet;
  } = $props();
</script>

<div class="block-head">
  <div class="sections" role="tablist" aria-label={label}>
    {#each names as name (name)}
      <button role="tab" aria-selected={current === name} class:current={current === name}
        onclick={() => (current = name)}>{title(name)}</button>
    {/each}
  </div>
  {@render aside?.()}
</div>

<style>
  .sections {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1.25rem;
  }
  /* The sections read as the heading they replace. */
  .sections button {
    font-family: var(--serif);
    font-size: 1.25rem;
    font-weight: 500;
    color: var(--muted);
    text-decoration: none;
    padding: 0 0 0.2rem;
    border-bottom: 2px solid transparent;
  }
  .sections button:hover {
    color: var(--text);
  }
  .sections button.current {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  @media (max-width: 640px) {
    .sections {
      gap: 0.25rem 1rem;
    }
    .sections button {
      font-size: 1.1rem;
    }
  }
</style>
