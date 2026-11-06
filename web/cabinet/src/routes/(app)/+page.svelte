<!-- My services: the account's service profiles, then the services it can
     ask for. Every type shows the same card; its own page is the type's. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, key } from '$lib/ui/format';

  type Data = { mine: Schemas['ServiceProfileOutput'][]; catalog: Schemas['ServiceOutput'][] };

  async function fetchAll(): Promise<Data> {
    const [mine, catalog] = await Promise.all([call(api.GET('/api/v1/me/services')),
      call(api.GET('/api/v1/services'))]);
    return { mine: mine.items, catalog: catalog.items };
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
</script>

<h1>{t('services.title')}</h1>
{#if notice}<p>{notice}</p>{/if}
<Async {load}>
  {#snippet children({ mine, catalog })}
    {#if mine.length}
      <div class="grid">
        {#each mine as profile (profile.id)}
          <a class="card service" href={'/services/' + profile.id}>
            <div class="row">
              <strong>{serviceName(profile.service)}</strong>
              <span class={'badge ' + (tone[profile.status] ?? '')}>{t('services.status_' + profile.status)}</span>
            </div>
            {#if profile.summary.title}<div>{profile.summary.title}</div>{/if}
            {#if profile.service === 'vpn'}
              <div class="muted">
                {t('vpn.devices_count', { count: profile.summary.devices ?? 0 })} ·
                {profile.summary.expires_at ? t('vpn.until', { date: date(String(profile.summary.expires_at)) })
                  : t('vpn.unlimited')}
              </div>
            {/if}
          </a>
        {/each}
      </div>
    {:else}
      <p class="muted">{t('services.none')}</p>
    {/if}

    {@const others = catalog.filter((item) => item.profiles && item.access !== 'active')}
    {#if others.length}
      <h2>{t('services.available')}</h2>
      <div class="grid">
        {#each others as item (item.service)}
          <div class="card">
            <strong>{serviceName(item.service)}</strong>
            <p class="muted">{t('services.access_' + item.access)}</p>
            {#if item.access === 'requestable'}
              <button class="primary" onclick={request}>{t('services.request')}</button>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  {/snippet}
</Async>

<style>
  .service {
    display: grid;
    gap: 0.4rem;
    color: inherit;
    text-decoration: none;
  }
  .service:hover {
    border-color: var(--accent);
  }
</style>
