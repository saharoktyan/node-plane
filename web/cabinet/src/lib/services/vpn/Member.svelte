<!-- VPN, the built-in service: term, tariff, traffic and devices in one line;
     what the tariff includes; the devices; getting a configuration for one
     of them on a server. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import type { ServiceProfile } from '$lib/services/registry';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';
  import Connect from './Connect.svelte';
  import Devices from './Devices.svelte';
  import { lines } from './limits';

  let { profile }: { profile: ServiceProfile } = $props();
  const path = () => ({ profile_id: profile.id });
  const fetchSummary = () => call(api.GET('/api/v1/me/profiles/{profile_id}/summary', { params: { path: path() } }));
  let load = $state(fetchSummary());
  let notice = $state('');

  let devices = $state<Schemas['DeviceList'] | null>(null);
  let devicesError = $state('');
  async function reloadDevices() {
    try {
      devices = await call(api.GET('/api/v1/profiles/{profile_id}/devices', { params: { path: path() } }));
      devicesError = '';
    } catch (error) {
      devicesError = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  reloadDevices();
  const active = $derived((devices?.items ?? []).filter((item) => item.status === 'active'));

  async function renew() {
    notice = '';
    try {
      await call(api.POST('/api/v1/me/profiles/{profile_id}/renewal', { params: { path: path() },
        headers: { 'Idempotency-Key': key() } }));
      notice = t('vpn.renewal_sent');
      load = fetchSummary();
    } catch (error) {
      notice = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
</script>

<Async {load}>
  {#snippet children(summary)}
    <div class="stats">
      <div>
        <div class="label">{t('vpn.term')}</div>
        <div class="value">{summary.expires_at ? date(summary.expires_at) : t('vpn.unlimited')}</div>
        {#if summary.renewal?.status === 'pending'}<div class="note">{t('vpn.renewal_pending')}</div>{/if}
      </div>
      <div>
        <div class="label">{t('vpn.tariff')}</div>
        <div class="value">{summary.tariff?.title ?? '—'}</div>
      </div>
      <div>
        <div class="label">{t('vpn.quota')}</div>
        {#if summary.quota}
          <div class="value">{bytes(Number(summary.quota.used_bytes))}</div>
          <div class="note">{t('vpn.quota_of', { limit: bytes(Number(summary.quota.limit_bytes)) })}</div>
          <div class="meter"><span style:width={`${Math.min(100, (100 * Number(summary.quota.used_bytes)) /
            Math.max(1, Number(summary.quota.limit_bytes)))}%`}></span></div>
        {:else}
          <div class="value">{t('vpn.no_quota')}</div>
        {/if}
      </div>
      <div>
        <div class="label">{t('vpn.devices')}</div>
        <div class="value">{devices ? active.length : (profile.summary.devices ?? 0)}</div>
      </div>
    </div>
    {#if profile.actions.includes('renewal') || notice}
      <div class="row actions">
        {#if profile.actions.includes('renewal')}<button onclick={renew}>{t('vpn.renew')}</button>{/if}
        {#if notice}<span>{notice}</span>{/if}
      </div>
    {/if}

    <section class="block">
      <h2>{t('vpn.included')}</h2>
      {#if summary.tariff}
        <p><strong>{summary.tariff.title}</strong></p>
        {#if summary.tariff.description}<p class="description">{summary.tariff.description}</p>{/if}
        {#if summary.tariff.duration_days}<p class="muted">{t('vpn.tariff_days', { days: summary.tariff.duration_days })}</p>{/if}
      {:else}
        <p class="muted">{t('vpn.no_tariff')}</p>
      {/if}
      <table class="limits">
        <tbody>
          {#each lines(summary.limits ?? []) as line (line.label)}
            <tr>
              <th scope="row">{line.label}</th>
              <td>{line.value}{#if line.own} <span class="badge">{t('vpn.own')}</span>{/if}</td>
            </tr>
          {/each}
          <tr>
            <th scope="row">{t('vpn.servers')}</th>
            <td>{summary.node_count}</td>
          </tr>
        </tbody>
      </table>
    </section>

    {#if devices}
      <Devices profileId={profile.id} list={devices} onchange={reloadDevices} />
      <Connect profileId={profile.id} nodes={summary.nodes} devices={active}
        issuing={profile.actions.includes('configs')} />
    {:else if devicesError}
      <p class="error">{devicesError}</p>
    {/if}
  {/snippet}
</Async>

<style>
  .meter {
    height: 4px;
    margin-top: 0.4rem;
    background: var(--line);
    border-radius: 2px;
    overflow: hidden;
    max-width: 12rem;
  }
  .meter span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .actions {
    margin: -0.75rem 0 1.5rem;
  }
  .description {
    white-space: pre-line;
    max-width: 48rem;
  }
  .limits th {
    width: 40%;
    max-width: 22rem;
  }
</style>
