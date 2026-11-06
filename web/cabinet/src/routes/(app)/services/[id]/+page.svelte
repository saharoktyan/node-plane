<!-- One service profile: the page of its type, from the registry. -->
<script lang="ts">
  import { page } from '$app/state';
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { moduleOf, serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';

  async function open(id: string) {
    const profile = (await call(api.GET('/api/v1/me/services'))).items.find((item) => item.id === id);
    if (!profile) throw new ApiError(404, 'resource_not_found', 'Not found');
    const panel = (await moduleOf(profile.service).member()).default;
    return { profile, panel };
  }

  const load = $derived(open(page.params.id ?? ''));
</script>

<nav class="crumbs"><a href="/">{t('services.title')}</a> /</nav>
<Async {load}>
  {#snippet children({ profile, panel: Panel })}
    <div class="page-head">
      <h1>{serviceName(profile.service)}</h1>
      <span class="badge">{t('services.status_' + profile.status)}</span>
    </div>
    <Panel {profile} />
  {/snippet}
</Async>

<style>
  .crumbs {
    color: var(--muted);
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
  }
</style>
