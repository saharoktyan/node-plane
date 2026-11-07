<!-- The relay as a service, as Workstation's Settings › Services keep it. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import RelaySettings from '$lib/admin/services/RelaySettings.svelte';
  import { t } from '$lib/i18n/index.svelte';

  // The nodes a relay can run on: those with an installed agent.
  let nodes = $state<{ key: string; title: string }[]>([]);
  call(api.GET('/api/v1/nodes', { params: { query: { limit: 100 } } })).then((list) => {
    nodes = (list.items as { key: string; title: string; flag?: string | null; stage?: string }[])
      .filter((node) => node.stage === 'node').map((node) => ({ key: node.key, title: [node.flag, node.title].filter(Boolean).join(' ') }));
  }).catch(() => {});
</script>

<nav class="crumbs"><a href="/admin/services">{t('admin.services')}</a></nav>
<div class="page-head"><h1>Relay</h1></div>
<section class="block appear"><RelaySettings {nodes} /></section>
