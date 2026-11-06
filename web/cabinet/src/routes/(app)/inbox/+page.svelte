<!-- The Inbox: announcements and notices, the same as in the bot. -->
<script lang="ts">
  import { api, call } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { dateTime } from '$lib/ui/format';

  type Item = { id: string; text: string; created_at: string; viewed_at: string | null };
  type Page = { items: Item[]; total: number };

  const fetchPage = async () => (await call(api.GET('/api/v1/me/notifications'))) as unknown as Page;
  let load = $state(fetchPage());

  async function dismiss(id: string) {
    await call(api.POST('/api/v1/me/notifications/{delivery_id}/dismiss', { params: { path: { delivery_id: id } } }));
    load = fetchPage();
  }
</script>

<h1>{t('inbox.title')}</h1>
<Async {load}>
  {#snippet children(inbox)}
    {#each inbox.items as item (item.id)}
      <article class="card" class:unread={!item.viewed_at}>
        <div class="muted">{dateTime(item.created_at)}</div>
        <p class="text">{item.text}</p>
        <button onclick={() => dismiss(item.id)}>{t('inbox.dismiss')}</button>
      </article>
    {:else}
      <p class="muted">{t('inbox.empty')}</p>
    {/each}
  {/snippet}
</Async>

<style>
  article {
    margin-bottom: 0.75rem;
  }
  .unread {
    border-left: 3px solid var(--accent);
  }
  .text {
    white-space: pre-wrap;
  }
</style>
