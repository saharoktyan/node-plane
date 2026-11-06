<!-- A ranking: one bar per item from a shared start, its name above and its
     value at the tip, so every number is read without pointing. -->
<script lang="ts">
  let { items, format, color = 'var(--series-1)', label }: {
    items: { label: string; value: number; note?: string; href?: string }[];
    format: (value: number) => string;
    color?: string;
    label: string;
  } = $props();

  const top = $derived(Math.max(1, ...items.map((item) => item.value)));
</script>

<ul class="bars" aria-label={label}>
  {#each items as item, index (index)}
    <li>
      <div class="name">
        {#if item.href}<a href={item.href}>{item.label}</a>{:else}<span>{item.label}</span>{/if}
        {#if item.note}<span class="muted">{item.note}</span>{/if}
      </div>
      <div class="track">
        <span class="bar" style:width={`calc((100% - 5.5rem) * ${item.value / top})`} style:background={color}></span>
        <span class="value">{format(item.value)}</span>
      </div>
    </li>
  {/each}
</ul>

<style>
  .bars {
    list-style: none;
    margin: 0.75rem 0 0;
    padding: 0;
    display: grid;
    align-content: start;
    gap: 0.6rem;
  }
  .name {
    display: flex;
    gap: 0.5rem;
    justify-content: space-between;
    font-size: 13px;
    min-width: 0;
  }
  .name > :first-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .muted {
    color: var(--muted);
    white-space: nowrap;
  }
  .track {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.2rem;
  }
  .bar {
    height: 8px;
    min-width: 2px;
    border-radius: 0 4px 4px 0;
  }
  .value {
    font-size: 12px;
    color: var(--soft);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
</style>
