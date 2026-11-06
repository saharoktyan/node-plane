<!-- VPN, the built-in service: term, traffic, tariff and devices in a row of
     tiles; then connecting and the devices, with what the tariff includes
     beside them. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import type { ServiceProfile } from '$lib/services/registry';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import Dialog from '$lib/ui/Dialog.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';
  import { tiles } from '$lib/ui/tiles';
  import Connect from './Connect.svelte';
  import Devices from './Devices.svelte';
  import { lines } from './limits';

  let { profile }: { profile: ServiceProfile } = $props();
  const path = () => ({ profile_id: profile.id });
  const fetchSummary = () => call(api.GET('/api/v1/me/profiles/{profile_id}/summary', { params: { path: path() } }));
  let load = $state(fetchSummary());
  let notice = $state('');
  let tariffOpen = $state(false);

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

  const used = (quota: Record<string, unknown>) =>
    Math.min(100, (100 * Number(quota.used_bytes)) / Math.max(1, Number(quota.limit_bytes)));
</script>

<Async {load}>
  {#snippet children(summary)}
    {@const facts = lines(summary.limits ?? [])}
    <div class="stats" use:tiles>
      <div>
        <div class="label">{t('vpn.term')}</div>
        <div class="value">{summary.expires_at ? date(summary.expires_at) : t('vpn.unlimited')}</div>
        {#if summary.renewal?.status === 'pending'}<div class="note">{t('vpn.renewal_pending')}</div>
        {:else if profile.actions.includes('renewal')}<button class="note" onclick={renew}>{t('vpn.renew')}</button>{/if}
        {#if notice}<div class="note">{notice}</div>{/if}
      </div>
      <div>
        <div class="label">{t('vpn.quota')}</div>
        {#if summary.quota}
          <div class="value">{bytes(Number(summary.quota.used_bytes))}</div>
          <div class="note">{t('vpn.quota_of', { limit: bytes(Number(summary.quota.limit_bytes)),
            date: date(String(summary.quota.period_end)) })}</div>
          <div class="meter"><span class:full={used(summary.quota) >= 90} style:width={`${used(summary.quota)}%`}></span></div>
        {:else}
          <div class="value">{t('vpn.no_quota')}</div>
        {/if}
      </div>
      {#if summary.tariff}
        <!-- The tariff opens its description over the page. -->
        <button class="cell" onclick={() => (tariffOpen = true)}>
          <div class="label">{t('vpn.tariff')}</div>
          <div class="value">{summary.tariff.title}</div>
          <div class="note">{t('vpn.about_tariff')} →</div>
        </button>
      {:else}
        <div>
          <div class="label">{t('vpn.tariff')}</div>
          <div class="value">—</div>
        </div>
      {/if}
      <div>
        <div class="label">{t('vpn.devices')}</div>
        <div class="value">{devices ? active.length : (profile.summary.devices ?? 0)}{#if devices?.device_limit != null}
          <small>&thinsp;/&thinsp;{devices.device_limit}</small>{/if}</div>
      </div>
    </div>

    <div class="columns">
      <div class="main">
        {#if devices}
          <Connect profileId={profile.id} nodes={summary.nodes} devices={active}
            issuing={profile.actions.includes('configs')} />
          <Devices profileId={profile.id} list={devices} onchange={reloadDevices} />
        {:else if devicesError}
          <p class="error">{devicesError}</p>
        {/if}
      </div>

      <aside>
        <div class="block-head">
          <h2>{summary.tariff ? t('vpn.tariff_named', { title: String(summary.tariff.title) }) : t('vpn.included')}</h2>
        </div>
        {#if !summary.tariff}<p class="muted">{t('vpn.no_tariff')}</p>
        {:else if summary.tariff.description}<p class="lead">{summary.tariff.description}</p>{/if}
        <dl class="facts">
          {#each facts as line, index (line.label)}
            <div>
              <dt>{line.label}</dt>
              <dd>{line.value}{#if line.own}<sup>*</sup>{/if}</dd>
            </div>
            <!-- The reset date follows the traffic: the period differs from the term. -->
            {#if index === 1 && summary.quota?.period_end}
              <div>
                <dt>{t('vpn.next_reset')}</dt>
                <dd><strong>{date(String(summary.quota.period_end))}</strong></dd>
              </div>
            {/if}
          {/each}
          <div>
            <dt>{t('vpn.servers')}</dt>
            <dd>{summary.node_count}</dd>
          </div>
          {#if summary.tariff?.duration_days}
            <div>
              <dt>{t('vpn.renewal')}</dt>
              <dd>{t('vpn.renewal_days', { days: summary.tariff.duration_days })}</dd>
            </div>
          {/if}
        </dl>
        {#if facts.some((line) => line.own)}<p class="footnote"><sup>*</sup> {t('vpn.own_note')}</p>{/if}
      </aside>
    </div>

    {#if summary.tariff}
      <Dialog bind:open={tariffOpen} title={String(summary.tariff.title)}>
        {#if summary.tariff.description}
          <p class="description">{summary.tariff.description}</p>
        {:else}
          <p class="muted">{t('vpn.no_description')}</p>
        {/if}
        {#if summary.tariff.duration_days}<p class="muted">{t('vpn.tariff_days', { days: summary.tariff.duration_days })}</p>{/if}
      </Dialog>
    {/if}
  {/snippet}
</Async>

<style>
  .meter {
    max-width: 14rem;
  }
  .meter span.full {
    background: var(--accent);
  }
  button.note {
    padding: 0.15rem 0 0;
    color: var(--text);
    font-weight: 600;
    text-decoration-color: var(--accent);
    text-decoration-thickness: 2px;
  }
  /* A tile that opens something: the whole tile is the button. */
  .stats > .cell {
    font: inherit;
    color: inherit;
    text-align: left;
    text-decoration: none;
    padding: 0.9rem 1.1rem;
  }
  .stats > .cell:hover {
    background: var(--panel);
    color: inherit;
  }
  .stats > .cell .note {
    color: var(--accent);
  }
  .lead {
    font-family: var(--serif);
    font-style: italic;
    font-size: 1.1rem;
    line-height: 1.5;
    white-space: pre-line;
    margin: 0.9rem 0 0;
  }
  sup {
    color: var(--accent);
    margin-left: 0.1rem;
  }
  .footnote {
    font-size: 12px;
    color: var(--muted);
  }
  .description {
    white-space: pre-line;
  }
</style>
