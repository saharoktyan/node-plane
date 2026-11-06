<script lang="ts">
  import '../app.css';
  import { onMount, type Snippet } from 'svelte';
  import { applyTheme, storedTheme } from '$lib/theme';

  let { children }: { children: Snippet } = $props();
  // On mocks, nothing asks the API before the service worker answers.
  let ready = $state(!__MOCK__);

  onMount(async () => {
    applyTheme(storedTheme());
    if (__MOCK__) {
      const { start } = await import('../mocks/browser');
      await start();
      ready = true;
    }
  });
</script>

{#if ready}
  {@render children()}
{/if}
