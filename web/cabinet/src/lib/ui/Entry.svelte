<!-- The frame of a page before sign-in. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  let { title, children }: { title: string; children: Snippet } = $props();
</script>

<main>
  <div class="card">
    <h1>{title}</h1>
    {@render children()}
  </div>
</main>

<style>
  main {
    max-width: 24rem;
    margin: 10vh auto;
    padding: 0 1rem;
  }
</style>
