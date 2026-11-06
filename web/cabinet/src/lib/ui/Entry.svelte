<!-- The frame of a page before sign-in. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  let { title, children }: { title: string; children: Snippet } = $props();
</script>

<main>
  <p class="brand">Node Plane</p>
  <h1>{title}</h1>
  {@render children()}
</main>

<style>
  main {
    max-width: 24rem;
    margin: 12vh auto;
    padding: 0 1rem;
  }
  h1 {
    margin-bottom: 1.75rem;
  }
  .brand {
    font-family: var(--serif);
    font-size: 1.15rem;
    font-weight: 600;
    margin: 0 0 2.5rem;
    padding-bottom: 0.6rem;
    border-bottom: 2px solid var(--text);
  }
</style>
