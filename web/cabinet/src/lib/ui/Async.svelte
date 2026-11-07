<!-- A value that loads: nothing while it does, so the page around it is
     drawn at once and the content fades in; the API's error in words if it
     fails. A value already at hand, or a promise read before, shows at once. -->
<script module lang="ts">
  type Outcome = { ok: true; value: unknown } | { ok: false; error: unknown };
  const settled = new WeakMap<Promise<unknown>, Outcome>();
</script>

<script lang="ts" generics="T">
  import type { Snippet } from 'svelte';
  import { ApiError } from '$lib/api/client';
  import { errorText } from '$lib/ui/errors';
  import { t } from '$lib/i18n/index.svelte';

  let { load, children }: { load: Promise<T> | T; children: Snippet<[T]> } = $props();

  const known = (source: Promise<T> | T): Outcome | null =>
    source instanceof Promise ? (settled.get(source) ?? null) : { ok: true, value: source };
  // Read at once, so a value at hand draws in the first frame; the effect follows changes.
  // svelte-ignore state_referenced_locally
  let outcome = $state<Outcome | null>(known(load));

  $effect(() => {
    const source = load;
    const found = known(source);
    outcome = found;
    if (found || !(source instanceof Promise)) return;
    source.then((value) => ({ ok: true, value }) as Outcome, (error) => ({ ok: false, error }) as Outcome).then((result) => {
      settled.set(source, result);
      if (load === source) outcome = result;
    });
  });
</script>

{#if outcome?.ok}
  {@render children(outcome.value as T)}
{:else if outcome}
  <p class="error">{outcome.error instanceof ApiError ? errorText(outcome.error) : t('common.error')}</p>
{/if}
