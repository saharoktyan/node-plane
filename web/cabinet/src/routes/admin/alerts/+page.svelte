<!-- Server alerts, as the bot sends them and Workstation lists them: what is
     wrong where and since when, each one marked seen once read, and whether
     the bot sends them, repeats them and says when they pass. -->
<script lang="ts">
  import { api, call, ApiError } from '$lib/api/client';
  import { REPEATS, describe, ordered, type Alert } from '$lib/admin/alerts';
  import { refreshAttention } from '$lib/admin/attention.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { dateTime } from '$lib/ui/format';
  import { toast } from '$lib/ui/toast.svelte';

  type Overview = { enabled: boolean; interval_minutes: number; repeat_minutes: number; notify_resolved: boolean;
    active_count: number; unacknowledged_count: number; active: Alert[];
    last_scan: { at?: string } | null; last_cpu_scan: { at?: string } | null };
  let overview = $state<Overview | null>(null);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      overview = (await call(api.GET('/api/v1/system/alerts'))) as unknown as Overview;
      failure = '';
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  // The checks run every few minutes; the list follows them.
  $effect(() => {
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  });

  async function run(work: () => Promise<unknown>, done?: string) {
    busy = true;
    failure = '';
    try {
      await work();
      if (done) toast(t(done));
      await load();
      await refreshAttention();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  const prefer = (changes: Partial<Pick<Overview, 'enabled' | 'repeat_minutes' | 'notify_resolved'>>) =>
    run(() => call(api.PATCH('/api/v1/system/alerts/preferences', { body: changes })), 'alerts.saved');
  const dismiss = (alert: Alert) => run(() => call(api.POST('/api/v1/system/alerts/{event_id}/dismiss',
    { params: { path: { event_id: alert.event_id! } } })));
  const when = (value: { at?: string } | null) => (value?.at ? dateTime(value.at) : t('alerts.never'));
  const text = (alert: Alert) => {
    const { key, reading } = describe(alert);
    return t('alerts.' + key) + (reading ? ' · ' + t('alerts.' + reading.key, { value: reading.value, cpus: reading.cpus ?? '—' }) : '');
  };
</script>

<div class="page-head"><h1>{t('alerts.title')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}
{#if overview}
  <section class="block appear">
    <div class="block-head"><h2>{t('alerts.open')}</h2>
      <span class="muted small">{t('alerts.summary', { active: overview.active_count, unread: overview.unacknowledged_count })}</span></div>
    {#if overview.active.length}
      <div class="rows">
        {#each ordered(overview.active) as alert (alert.node_key + alert.kind)}
          <div class:seen={alert.dismissed}>
            <span class="grow">
              <strong><a href={'/admin/nodes/' + alert.node_key}>{[alert.flag, alert.title].filter(Boolean).join(' ')}</a></strong>
              <span class="what">{text(alert)}</span>
              <span class="muted small">{t('alerts.since', { time: dateTime(alert.first_seen_at) })}
                {#if alert.dismissed}· {t('alerts.seen')}{/if}</span>
            </span>
            {#if !alert.dismissed && alert.event_id}
              <button disabled={busy} onclick={() => dismiss(alert)}>{t('alerts.dismiss')}</button>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <p class="muted">{t('alerts.none')}</p>
    {/if}
    <p class="muted small checks">{t('alerts.checks', { full: when(overview.last_scan), cpu: when(overview.last_cpu_scan) })}</p>
  </section>

  <div class="setting appear">
    <div><h2>{t('alerts.sending')}</h2><p class="muted">{t('alerts.sending_hint', { minutes: overview.interval_minutes })}</p></div>
    <div class="controls">
      <label class="check"><input type="checkbox" checked={overview.enabled} disabled={busy}
        onchange={(event) => prefer({ enabled: event.currentTarget.checked })} /> {t('alerts.enabled')}</label>
      {#if overview.enabled}
        <label>{t('alerts.repeat')}
          <select value={overview.repeat_minutes} disabled={busy}
            onchange={(event) => prefer({ repeat_minutes: Number(event.currentTarget.value) })}>
            {#each REPEATS as minutes (minutes)}<option value={minutes}>{t('alerts.repeat_' + minutes)}</option>{/each}
          </select>
        </label>
        <label class="check"><input type="checkbox" checked={overview.notify_resolved} disabled={busy}
          onchange={(event) => prefer({ notify_resolved: event.currentTarget.checked })} /> {t('alerts.notify_resolved')}</label>
      {/if}
    </div>
  </div>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  .rows .grow {
    display: grid;
    gap: 0.15rem;
  }
  .seen .what {
    color: var(--muted);
  }
  .checks {
    margin: 0.75rem 0 0;
  }
  .controls {
    display: grid;
    gap: 0.9rem;
    align-content: start;
  }
  .controls select {
    max-width: 16rem;
  }
</style>
