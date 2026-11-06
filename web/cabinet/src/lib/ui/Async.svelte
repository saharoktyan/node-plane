<!-- A value that loads: a spinner line while it does, the API's error
     in words if it fails, and the content with the value when it is there. -->
<script lang="ts" generics="T">
  import type { Snippet } from 'svelte';
  import { ApiError } from '$lib/api/client';
  import { errorText } from '$lib/ui/errors';
  import { t } from '$lib/i18n/index.svelte';

  let { load, children }: { load: Promise<T>; children: Snippet<[T]> } = $props();
</script>

{#await load}
  <p class="muted">{t('common.loading')}</p>
{:then value}
  {@render children(value)}
{:catch error}
  <p class="error">{error instanceof ApiError ? errorText(error) : t('common.error')}</p>
{/await}
