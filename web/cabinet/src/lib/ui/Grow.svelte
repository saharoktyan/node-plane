<!-- A place whose height follows what is in it: it opens from nothing,
     eases to each new height as its content changes, and folds away; only
     then is `onclosed` called, so the content can stay while it folds. -->
<script lang="ts">
  import type { Snippet } from 'svelte';

  let { open, onclosed, children }: { open: boolean; onclosed?: () => void; children: Snippet } = $props();
  let inner = $state<HTMLDivElement>();
  let height = $state(0);

  $effect(() => {
    if (!inner) return;
    // Growing follows at once; shrinking waits a moment, so content that
    // is replaced and still loading eases from the old height straight to
    // the new one instead of dipping to nothing first.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const observer = new ResizeObserver(() => {
      const next = inner!.offsetHeight;
      clearTimeout(timer);
      if (next >= height) height = next;
      else timer = setTimeout(() => (height = inner!.offsetHeight), 250);
    });
    observer.observe(inner);
    return () => {
      clearTimeout(timer);
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
