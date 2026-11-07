<!-- The web cabinet as a service, as Workstation's Settings › Services keep it. -->
<script lang="ts">
  import { goto } from '$app/navigation';
  import CabinetSettings from '$lib/admin/services/CabinetSettings.svelte';
  import { t } from '$lib/i18n/index.svelte';
</script>

<nav class="crumbs"><a href="/admin/services">{t('admin.services')}</a></nav>
<div class="page-head"><h1>{t('services.name_web_cabinet')}</h1></div>
<section class="block appear"><CabinetSettings onrelay={() => goto('/admin/services/relay')} /></section>
