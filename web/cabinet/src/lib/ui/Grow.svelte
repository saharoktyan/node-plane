<!-- A place whose height follows what is in it: it opens from nothing,
     eases to each new height as its content changes, and folds away; only
     then is `onclosed` called, so the content can stay while it folds.
     When `key` changes the content is replaced: the height holds until the
     new content has loaded (changed once after it is drawn), so it eases
     from the old height straight to the new one. -->
<script lang="ts">
  import type { Snippet } from 'svelte';

  let { open, key = null, onclosed, children }: {
    open: boolean; key?: unknown; onclosed?: () => void; children: Snippet;
  } = $props();
  let inner = $state<HTMLDivElement>();
  let height = $state(0);
  // After a replacement: the first size is the new content still loading.
  let swapped = false;
  let first = false;
  let release: ReturnType<typeof setTimeout> | undefined;

  $effect.pre(() => {
    void key;
    swapped = first = true;
    clearTimeout(release);
    release = setTimeout(() => {
      swapped = false;
      if (inner) height = inner.offsetHeight;
    }, 1500);
  });

  $effect(() => {
    if (!inner) return;
    const observer = new ResizeObserver(() => {
      const next = inner!.offsetHeight;
      if (swapped && first) {
        first = false;
        if (next > height) height = next;
        return;
      }
      swapped = false;
      clearTimeout(release);
      height = next;
    });
    observer.observe(inner);
    return () => {
      clearTimeout(release);
      observer.disconnect();
    };
  });
</script>

<div class="grow" class:open style:height={open ? height + 'px' : '0px'} inert={!open}
  ontransitionend={(event) => { if (['height', 'opacity'].includes(event.propertyName) && !open) onclosed?.(); }}>
  <div class="inner" bind:this={inner}>{@render children()}</div>
</div>

<style>
  .grow {
    overflow: hidden;
    opacity: 0;
    transition: height 0.35s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.3s ease;
  }
  .grow.open {
    opacity: 1;
  }
  .inner {
    display: flow-root;
  }
  @media (prefers-reduced-motion: reduce) {
    .grow {
      transition: opacity 0.15s;
    }
  }
</style>
