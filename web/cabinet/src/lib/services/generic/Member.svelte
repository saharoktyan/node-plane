<!-- A service from a manifest: what the type declares, without code of its
     own. Until stage I it shows the profile's status and summary; the
     connection fields and actions a manifest declares come with it. -->
<script lang="ts">
  import type { ServiceProfile } from '$lib/services/registry';
  import { t } from '$lib/i18n/index.svelte';

  let { profile }: { profile: ServiceProfile } = $props();
  const entries = $derived(Object.entries(profile.summary).filter(([, value]) => value !== null));
</script>

<div class="card">
  <p><span class="badge">{t('services.status_' + profile.status)}</span></p>
  {#if entries.length}
    <table>
      <tbody>
        {#each entries as [name, value] (name)}
          <tr><th>{name}</th><td>{value}</td></tr>
        {/each}
      </tbody>
    </table>
  {/if}
  {#if profile.actions.length}
    <div class="row">
      {#each profile.actions as action (action)}
        <button disabled>{action}</button>
      {/each}
    </div>
  {/if}
</div>
