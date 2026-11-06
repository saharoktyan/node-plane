<!-- My services: the account's service profiles, then the services it can
     ask for. Every type has the same row; its own page is the type's. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { serviceName } from '$lib/services/registry';
  import Async from '$lib/ui/Async.svelte';
  import Icon from '$lib/ui/Icon.svelte';
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

  function detail(profile: Schemas['ServiceProfileOutput']) {
    if (profile.service !== 'vpn') return Object.values(profile.summary).filter(Boolean).join(' · ');
    const term = profile.summary.expires_at ? t('vpn.until', { date: date(String(profile.summary.expires_at)) })
      : t('vpn.unlimited');
    return [t('vpn.devices_count', { count: profile.summary.devices ?? 0 }), profile.summary.tariff, term]
      .filter(Boolean).join(' · ');
  }
</script>

<div class="page-head"><h1>{t('services.title')}</h1></div>
{#if notice}<p>{notice}</p>{/if}
<Async {load}>
  {#snippet children({ mine, catalog })}
    <section class="block">
      {#if mine.length}
        <div class="rows">
          {#each mine as profile (profile.id)}
            <a href={'/services/' + profile.id}>
              <strong class="name">{serviceName(profile.service)}</strong>
              <span class={'badge ' + (tone[profile.status] ?? '')}>{t('services.status_' + profile.status)}</span>
              <span class="grow muted">{detail(profile)}</span>
              <Icon name="expand" size={18} />
            </a>
          {/each}
        </div>
      {:else}
        <p class="muted">{t('services.none')}</p>
      {/if}
    </section>

    {@const others = catalog.filter((item) => item.profiles && item.access !== 'active')}
    {#if others.length}
      <section class="block">
        <h2>{t('services.available')}</h2>
        <div class="rows">
          {#each others as item (item.service)}
            <div>
              <strong class="name">{serviceName(item.service)}</strong>
              <span class="grow muted">{t('services.access_' + item.access)}</span>
              {#if item.access === 'requestable'}
                <button class="primary" onclick={request}>{t('services.request')}</button>
              {/if}
            </div>
          {/each}
        </div>
      </section>
    {/if}
  {/snippet}
</Async>

<style>
  .name {
    min-width: 8rem;
  }
</style>
