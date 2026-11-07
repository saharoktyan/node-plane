<!-- Temporary configurations: access to one server for half a day to three
     days, not tied to anyone's profile. -->
<script lang="ts">
  import Temporary from '$lib/admin/Temporary.svelte';
  import { t } from '$lib/i18n/index.svelte';
</script>

<div class="page-head"><h1>{t('temporary.title')}</h1></div>
<p class="muted small lead">{t('temporary.lead')}</p>
<Temporary />

<style>
  .lead {
    margin-top: -0.5rem;
    max-width: 44rem;
    font-size: 13px;
  }
</style>
