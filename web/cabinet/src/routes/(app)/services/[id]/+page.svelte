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

<p><a href="/">← {t('services.title')}</a></p>
<Async {load}>
  {#snippet children({ profile, panel: Panel })}
    <h1>{serviceName(profile.service)}</h1>
    <Panel {profile} />
  {/snippet}
</Async>
