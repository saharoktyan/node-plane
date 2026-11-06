<!-- VPN, the built-in service: term, tariff, quota, traffic and the
     servers the profile reaches; renewal when the term ends. Devices,
     configurations and QR codes come next. -->
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
    <div class="card">
      <table>
        <tbody>
          <tr><th>{t('vpn.status')}</th><td>{t('services.status_' + profile.status)}</td></tr>
          <tr><th>{t('vpn.term')}</th>
            <td>{summary.expires_at ? t('vpn.until', { date: date(summary.expires_at) }) : t('vpn.unlimited')}</td></tr>
          {#if summary.tariff}<tr><th>{t('vpn.tariff')}</th><td>{summary.tariff.title}</td></tr>{/if}
          {#if summary.quota}
            <tr><th>{t('vpn.quota')}</th>
              <td>{t('vpn.quota_used', { used: bytes(Number(summary.quota.used_bytes)),
                limit: bytes(Number(summary.quota.limit_bytes)) })}</td></tr>
          {/if}
          {#if summary.renewal?.status === 'pending'}
            <tr><th>{t('vpn.renewal')}</th><td>{t('vpn.renewal_pending')}</td></tr>
          {/if}
        </tbody>
      </table>
      {#if profile.actions.includes('renewal')}
        <p><button onclick={renew}>{t('vpn.renew')}</button></p>
      {/if}
      {#if notice}<p>{notice}</p>{/if}
    </div>

    <h2>{t('vpn.servers')}</h2>
    {#if summary.nodes.length}
      <div class="grid">
        {#each summary.nodes as node (node.key)}
          <div class="card">
            <strong>{node.flag} {node.title}</strong>
            <div class="muted">{node.protocols.map((protocol) => t('vpn.protocol_' + protocol)).join(', ')}</div>
          </div>
        {/each}
      </div>
    {:else}
      <p class="muted">{t('vpn.no_servers')}</p>
    {/if}
  {/snippet}
</Async>
