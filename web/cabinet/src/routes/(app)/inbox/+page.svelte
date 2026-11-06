<!-- The Inbox: announcements and notices, the same as in the bot. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { dateTime } from '$lib/ui/format';

  type Item = { id: string; title?: string | null; text: string; created_at: string; viewed_at: string | null };
  type Page = { items: Item[]; total: number };

  const fetchPage = async () => (await call(api.GET('/api/v1/me/notifications'))) as unknown as Page;
  let load = $state(fetchPage());

  async function dismiss(id: string) {
    await call(api.POST('/api/v1/me/notifications/{delivery_id}/dismiss', { params: { path: { delivery_id: id } } }));
    load = fetchPage();
  }
</script>

<div class="page-head"><h1>{t('inbox.title')}</h1></div>
<Async {load}>
  {#snippet children(inbox)}
    <div class="rows">
      {#each inbox.items as item (item.id)}
        <div class:unread={!item.viewed_at}>
          <span class="when muted">{dateTime(item.created_at)}</span>
          <div class="grow">
            {#if item.title}<strong class="title">{item.title}</strong>{/if}
            <p class="text">{item.text}</p>
          </div>
          <button onclick={() => dismiss(item.id)}>{t('inbox.dismiss')}</button>
        </div>
      {:else}
        <p class="muted">{t('inbox.empty')}</p>
      {/each}
    </div>
  {/snippet}
</Async>

<style>
  .unread {
    box-shadow: inset 3px 0 0 var(--accent);
    padding-left: 0.9rem !important;
  }
  .when {
    min-width: 11rem;
    font-size: 0.9rem;
  }
  .title {
    display: block;
    margin-bottom: 0.2rem;
  }
  .text {
    white-space: pre-wrap;
    margin: 0;
  }
</style>
