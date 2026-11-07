<!-- What holds for all of VPN, as the bot's policies: whether traffic is
     counted, and how many servers have the current connection and speed
     limits, with those that cannot apply them. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';
  import { toast } from '$lib/ui/toast.svelte';

  type Traffic = { enabled: boolean; interval_minutes: number; last_scan: string | null };
  type Delivery = { items: { node_key: string; title: string; flag?: string; status: string;
    protocols?: { protocol: string; state: string }[] }[] };
  let traffic = $state<Traffic | null>(null);
  let delivery = $state<{ name: string; value: Delivery }[]>([]);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      const [found, connections, speed] = await Promise.all([call(api.GET('/api/v1/system/traffic')),
        call(api.GET('/api/v1/connection-limits/nodes')).catch(() => null),
        call(api.GET('/api/v1/speed-limits/nodes')).catch(() => null)]);
      traffic = found as unknown as Traffic;
      delivery = [['connections', connections], ['speed', speed]].filter(([, value]) => value)
        .map(([name, value]) => ({ name: name as string, value: value as unknown as Delivery }));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  load();

  async function count(enabled: boolean) {
    busy = true;
    failure = '';
    try {
      traffic = (await call(api.PATCH('/api/v1/system/traffic/preferences', { body: { enabled } }))) as unknown as Traffic;
      toast(t(enabled ? 'vpnadmin.traffic_on' : 'vpnadmin.traffic_off'));
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  // A server that refused or cannot run the limits, as Workstation counts it.
  const unable = (item: Delivery['items'][number]) => (item.protocols ?? []).some((p) => ['failed', 'unsupported'].includes(p.state));
</script>

{#if failure}<p class="error">{failure}</p>{/if}
{#if traffic}
  <div class="setting">
    <div><h2>{t('vpnadmin.traffic')}</h2><p class="muted">{t('vpnadmin.traffic_hint')}</p></div>
    <div class="controls">
      <label class="check"><input type="checkbox" checked={traffic.enabled} disabled={busy || !account.may('settings.manage')}
        onchange={(event) => count(event.currentTarget.checked)} /> {t('vpnadmin.traffic_count')}</label>
      <p class="muted small">{traffic.enabled ? (traffic.last_scan
        ? t('vpnadmin.traffic_last', { time: dateTime(traffic.last_scan), minutes: traffic.interval_minutes })
        : t('vpnadmin.traffic_waiting', { minutes: traffic.interval_minutes })) : t('vpnadmin.traffic_paused')}</p>
    </div>
  </div>
{/if}
{#if delivery.length}
  <div class="setting">
    <div><h2>{t('vpnadmin.delivery')}</h2><p class="muted">{t('vpnadmin.delivery_hint')}</p></div>
    <div class="controls">
      {#each delivery as { name, value } (name)}
        {@const current = value.items.filter((item) => item.status === 'current').length}
        {@const stuck = value.items.filter(unable)}
        <div>
          <strong>{t('vpnadmin.delivery_' + name)}</strong>
          <p class="muted small">{value.items.length ? t('vpnadmin.delivery_line', { current, total: value.items.length })
            : t('vpnadmin.delivery_none')}</p>
          {#if stuck.length}
            <p class="status danger small">{t('vpnadmin.delivery_unable', { nodes: stuck.map((item) =>
              [item.flag, item.title].filter(Boolean).join(' ')).join(', ') })}</p>
          {/if}
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .controls {
    display: grid;
    gap: 0.75rem;
    align-content: start;
  }
  .controls p {
    margin: 0.2rem 0 0;
  }
  .small {
    font-size: 13px;
  }
  .status.danger {
    color: var(--danger);
  }
</style>
