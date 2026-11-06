<!-- A service from a manifest: what the type declares, without code of its
     own. Until stage I it shows the profile's summary; the connection fields
     and actions a manifest declares come with it. -->
<script lang="ts">
  import type { ServiceProfile } from '$lib/services/registry';
  import { tiles } from '$lib/ui/tiles';

  let { profile }: { profile: ServiceProfile } = $props();
  const entries = $derived(Object.entries(profile.summary).filter(([, value]) => value !== null));
</script>

{#if entries.length}
  <div class="stats" use:tiles>
    {#each entries as [name, value] (name)}
      <div><div class="label">{name}</div><div class="value small">{value}</div></div>
    {/each}
  </div>
{/if}
{#if profile.actions.length}
  <div class="row">
    {#each profile.actions as action (action)}<button disabled>{action}</button>{/each}
  </div>
{/if}

<style>
  .small {
    font-size: 1rem;
  }
</style>
