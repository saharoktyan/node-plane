<!-- VPN, the built-in service: term, tariff and traffic in one line, then
     the servers the profile reaches. Devices, configurations, QR codes and
     charts come next. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import type { ServiceProfile } from '$lib/services/registry';
  import { t } from '$lib/i18n/index.svelte';
  import Async from '$lib/ui/Async.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, date, key } from '$lib/ui/format';

  let { profile }: { profile: ServiceProfile } = $props();
  const fetchSummary = () =>
    call(api.GET('/api/v1/me/profiles/{profile_id}/summary', { params: { path: { profile_id: profile.id } } }));
  let load = $state(fetchSummary());
  let notice = $state('');

  async function renew() {
    notice = '';
    try {
      await call(api.POST('/api/v1/me/profiles/{profile_id}/renewal', {
        params: { path: { profile_id: profile.id } }, headers: { 'Idempotency-Key': key() } }));
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
        <div class="value">{profile.summary.devices ?? 0}</div>
      </div>
    </div>
    {#if profile.actions.includes('renewal') || notice}
      <div class="row actions">
        {#if profile.actions.includes('renewal')}<button onclick={renew}>{t('vpn.renew')}</button>{/if}
        {#if notice}<span>{notice}</span>{/if}
      </div>
    {/if}

    <section class="block">
      <h2>{t('vpn.servers')}</h2>
      {#if summary.nodes.length}
        <table>
          <thead><tr><th>{t('vpn.server')}</th><th>{t('vpn.region')}</th><th>{t('vpn.protocols')}</th></tr></thead>
          <tbody>
            {#each summary.nodes as node (node.key)}
              <tr>
                <td>{node.flag} {node.title}</td>
                <td class="muted">{node.region || '—'}</td>
                <td>{node.protocols.map((protocol) => t('vpn.protocol_' + protocol)).join(', ')}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <p class="muted">{t('vpn.no_servers')}</p>
      {/if}
    </section>
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
</style>
