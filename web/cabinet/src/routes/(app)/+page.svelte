<!-- Home: one sentence on what matters now, the account's services with their
     numbers, the latest of the Inbox beside them, and services it can ask for. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { count, lead, when } from '$lib/home/lead';
  import { i18n, t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';

  type Message = { id: string; title?: string | null; text: string; created_at: string; viewed_at: string | null };
  type Data = { mine: Schemas['ServiceProfileOutput'][]; catalog: Schemas['ServiceOutput'][]; inbox: Message[] | null };

  async function fetchAll(): Promise<Data> {
    // The Inbox is beside the services; without it the page still opens.
    const inbox = call(api.GET('/api/v1/me/notifications', { params: { query: { limit: 3 } } }))
      .then((page) => (page as unknown as { items: Message[] }).items).catch(() => null);
    const [mine, catalog] = await Promise.all([call(api.GET('/api/v1/me/services')), call(api.GET('/api/v1/services'))]);
    return { mine: mine.items, catalog: catalog.items, inbox: await inbox };
  }

  let load = $state(fetchAll());
  let notice = $state('');

  async function request() {
    notice = '';
    try {
      await call(api.POST('/api/v1/me/access-requests', { params: { header: { 'Idempotency-Key': key() } } }));
      notice = t('services.requested');
      load = fetchAll();
    } catch (error) {
      notice = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  const tone: Record<string, string> = { active: 'ok', frozen: 'warn', expired: 'danger', suspended: 'warn' };
  const today = $derived(new Date().toLocaleDateString(i18n.language, { weekday: 'long', day: 'numeric', month: 'long' }));
  const name = $derived(account.me?.display_name ?? account.me?.login ?? '');

  // A message's heading: its title, else its first line.
  const heading = (message: Message) => message.title || message.text.split('\n')[0];
  const body = (message: Message) => (message.title ? message.text : message.text.split('\n').slice(1).join(' '));
  const used = (summary: Record<string, unknown>) =>
    Math.min(100, (100 * Number(summary.quota_used_bytes)) / Math.max(1, Number(summary.quota_limit_bytes)));
</script>

<Async {load}>
  {#snippet children({ mine, catalog, inbox })}
    {@const others = catalog.filter((item) => item.profiles && item.access !== 'active')}
    <div class="caps">{today}</div>
    <p class="lead">{lead(mine, name)}</p>
    {#if notice}<p>{notice}</p>{/if}

    <div class="columns">
      <div class="main">
        {#if mine.length}
          <section class="block">
            <div class="block-head"><h2>{t('home.services')} <small>{count(mine.length)}</small></h2></div>
            <div class="rows">
              {#each mine as profile (profile.id)}
                {@const summary = profile.summary}
                <a href={'/services/' + profile.id} aria-label={t('home.open', { name: serviceName(profile.service) })}>
                  <div class="title">
                    <div><span class="name">{serviceName(profile.service)}</span>
                      <span class={'badge ' + (tone[profile.status] ?? '')}>{t('services.status_' + profile.status)}</span></div>
                    {#if summary.tariff}<div class="muted">{summary.tariff}</div>{/if}
                  </div>
                  {#if profile.service === 'vpn'}
                    <div class="figure">
                      <div class="caps">{t('home.term')}</div>
                      <div class="number">{summary.expires_at ? date(String(summary.expires_at)) : t('home.unlimited')}</div>
                    </div>
                    <div class="figure wide">
                      <div class="caps">{t('home.traffic')}</div>
                      {#if summary.quota_limit_bytes != null}
                        <div class="number">{bytes(Number(summary.quota_used_bytes))}
                          <small>/ {bytes(Number(summary.quota_limit_bytes))}</small></div>
                        <div class="meter"><span style:width={`${used(summary)}%`}></span></div>
                      {:else}
                        <div class="number">{t('home.no_quota')}</div>
                      {/if}
                    </div>
                    <div class="figure">
                      <div class="caps">{t('home.devices')}</div>
                      <div class="number">{summary.devices ?? 0}</div>
                    </div>
                  {:else}
                    <div class="figure wide muted">{Object.values(summary).filter(Boolean).join(' · ')}</div>
                  {/if}
                  <span class="arrow" aria-hidden="true">→</span>
                </a>
              {/each}
            </div>
          </section>
        {/if}

        {#if others.length}
          <section class="block">
            <div class="block-head"><h2>{t('services.available')}</h2></div>
            <div class="rows">
              {#each others as item (item.service)}
                <div>
                  <span class="grow"><span class="name small">{serviceName(item.service)}</span>
                    <span class="muted">{t('services.access_' + item.access)}</span></span>
                  {#if item.access === 'requestable'}
                    <button class="primary" onclick={request}>{t('services.request')}</button>
                  {/if}
                </div>
              {/each}
            </div>
          </section>
        {/if}
      </div>

      {#if inbox}
        <aside>
          <div class="block-head"><h2>{t('home.inbox')}</h2><a href="/inbox" class="muted">{t('home.all')}</a></div>
          {#each inbox as message (message.id)}
            <a class="message" href="/inbox">
              <div class="meta">
                {#if !message.viewed_at}<span class="new">{t('home.new')}</span>{/if}
                <span class="muted">{when(message.created_at)}</span>
              </div>
              <div class="heading">{heading(message)}</div>
              {#if body(message)}<div class="body">{body(message)}</div>{/if}
            </a>
          {:else}
            <p class="muted">{t('home.inbox_empty')}</p>
          {/each}
        </aside>
      {/if}
    </div>
  {/snippet}
</Async>

<style>
  .lead {
    font-family: var(--serif);
    font-size: 1.95rem;
    line-height: 1.3;
    letter-spacing: -0.01em;
    max-width: 52rem;
    margin: 0.5rem 0 2.25rem;
  }
  .rows > a {
    gap: 1rem 2rem;
    padding: 1.25rem 0;
  }
  .title {
    flex: 1 1 12rem;
    min-width: 0;
  }
  .name {
    font-family: var(--serif);
    font-size: 1.6rem;
    margin-right: 0.6rem;
  }
  .name.small {
    font-size: 1.2rem;
  }
  .figure {
    flex: 0 1 9rem;
    min-width: 0;
  }
  .figure.wide {
    flex-basis: 13rem;
  }
  .number {
    font-family: var(--serif);
    font-size: 1.25rem;
    margin-top: 0.2rem;
  }
  .number small {
    font-size: 0.85rem;
    color: var(--muted);
  }
  .arrow {
    font-size: 1.25rem;
    color: var(--muted);
  }
  .block-head a {
    font-size: 13px;
  }
  .message {
    display: block;
    padding: 0.85rem 0;
    border-bottom: 1px solid var(--line);
    color: inherit;
    text-decoration: none;
  }
  .message:hover .heading {
    color: var(--accent);
  }
  .meta {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }
  .new {
    color: var(--accent);
    font-weight: 600;
  }
  .heading {
    font-weight: 600;
    margin-top: 0.2rem;
  }
  .body {
    color: var(--soft);
    margin-top: 0.15rem;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  @media (max-width: 760px) {
    .lead {
      font-size: 1.45rem;
    }
    .arrow {
      display: none;
    }
  }
</style>
