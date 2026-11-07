<!-- VPN as a service, as the bot's policies and Workstation keep it: the
     installation's limits, tariffs, what a new server starts with, and
     traffic counting with the limits' delivery to servers. -->
<script lang="ts">
  import { page } from '$app/state';
  import Policies from '$lib/admin/Policies.svelte';
  import Defaults from '$lib/admin/services/Defaults.svelte';
  import General from '$lib/admin/services/General.svelte';
  import Tariffs from '$lib/admin/services/Tariffs.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Sections from '$lib/ui/Sections.svelte';

  type Section = 'limits' | 'tariffs' | 'defaults' | 'general';
  const SECTIONS: Section[] = ['limits', 'tariffs', 'defaults', 'general'];
  // A card's way back names its section.
  const asked = page.url.searchParams.get('section') as Section | null;
  let section = $state<Section>(asked && SECTIONS.includes(asked) ? asked : 'limits');
</script>

<nav class="crumbs"><a href="/admin/services">{t('admin.services')}</a></nav>
<div class="page-head"><h1>VPN</h1></div>
<section class="block appear">
  <Sections names={SECTIONS} bind:current={section} label={t('vpnadmin.sections')} title={(name) => t('vpnadmin.section_' + name)} />
  <!-- Every section loads with the page and stays, so switching only shows another. -->
  <div hidden={section !== 'limits'}>
    <p class="muted small">{t('vpnadmin.limits_hint')}</p>
    <Policies subject={{ kind: 'installation' }} bare />
  </div>
  <div hidden={section !== 'tariffs'}><Tariffs /></div>
  <div hidden={section !== 'defaults'}><Defaults /></div>
  <div hidden={section !== 'general'}><General /></div>
</section>

<style>
  .small {
    font-size: 13px;
  }
</style>
