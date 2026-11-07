/** Announcements and alerts on mocks: a delivery that goes on for a few
 * reads, and server alerts that can be marked seen. */
import { http, HttpResponse } from 'msw';
import type { Schemas } from '$lib/api/client';
import { error, guarded } from './people';
import { localized } from './localized';
import { choice } from './scenario';

type Job = { id: string; status: 'running' | 'completed'; total: number; counts: Record<string, number>; reads: number };
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
const counts = (sent: number, extra: Record<string, number> = {}) =>
  ({ queued: 0, claimed: 0, sent, failed: 0, unknown: 0, skipped: 0, inbox_only: 0, ...extra });

let state: { scenario: string; jobs: Job[]; alerts: { enabled: boolean; interval_minutes: number; repeat_minutes: number;
  notify_resolved: boolean }; active: Record<string, unknown>[]; dismissed: string[] } | null = null;

function store() {
  const { scenario } = choice();
  if (state?.scenario === scenario) return state;
  const empty = scenario === 'empty';
  const alert = (node_key: string, title: string, flag: string, kind: string, minutes: number, payload = {}) =>
    ({ node_key, title, flag, kind, first_seen_at: ago(minutes), last_seen_at: ago(1), event_id: crypto.randomUUID(), payload });
  state = { scenario, dismissed: [],
    jobs: empty ? [] : [{ id: crypto.randomUUID(), status: 'completed', total: 14, counts: counts(11, { failed: 1, inbox_only: 2 }), reads: 0 }],
    alerts: { enabled: !empty, interval_minutes: 5, repeat_minutes: 60, notify_resolved: true },
    active: empty ? [] : [
      alert('jp1-0', 'Tokyo', '🇯🇵', 'disk_low', 95, { value: 7.24 }),
      alert('nl1-2', 'Amsterdam 3', '🇳🇱', 'awg_down', 22),
      alert('de1-0', 'Frankfurt 1', '🇩🇪', 'load_high', 300, { value: 3.418, cpus: 2 })
    ] };
  // One was seen already.
  state.dismissed.push(state.active[2]?.event_id as string);
  return state;
}

const active = () => store().active.map((item) => ({ ...item, dismissed: store().dismissed.includes(item.event_id as string) }));
export const unseenAlerts = () => (store().alerts.enabled ? active().filter((item) => !item.dismissed).length : 0);

// A delivery under way sends a few more on each read.
function read(job: Job) {
  if (job.status === 'running' && ++job.reads >= 3) {
    job.counts = counts(job.total - 2, { inbox_only: 2 });
    job.status = 'completed';
  } else if (job.status === 'running') {
    job.counts.sent += 3;
    job.counts.queued = Math.max(0, job.total - job.counts.sent);
  }
  const { reads: _, ...output } = job;
  return output;
}

export const systemHandlers = [
  http.get('/api/v1/announcements', guarded(() => ({ last_job: store().jobs[0] ? read(store().jobs[0]) : null }))),
  http.get('/api/v1/announcements/:id', guarded((_, { id }) => {
    const job = store().jobs.find((item) => item.id === id);
    return job ? read(job) : error(404, 'resource_not_found');
  })),
  http.post('/api/v1/announcements/preview', guarded(async (request) => {
    const body = (await request.json()) as { texts: Schemas['LocalizedTextInput']; titles?: Schemas['LocalizedTextInput'] | null };
    return { texts: localized(body.texts), titles: body.titles ? localized(body.titles) : null,
      recipients: 14, telegram: 12, languages: { en: 3, ru: 11 } };
  })),
  http.post('/api/v1/announcements', guarded(() => {
    const job: Job = { id: crypto.randomUUID(), status: 'running', total: 14, counts: counts(0, { queued: 14 }), reads: 0 };
    store().jobs.unshift(job);
    return HttpResponse.json(read(job), { status: 202 });
  })),

  http.get('/api/v1/system/alerts', guarded(() => {
    const items = active();
    return { ...store().alerts, active: items, active_count: items.length,
      unacknowledged_count: items.filter((item) => !item.dismissed).length,
      last_scan: { at: ago(3) }, last_cpu_scan: { at: ago(1) }, cpu_sample_seconds: 5, cpu_window_seconds: 60,
      cpu_check_seconds: 30, delivery_counts: counts(9) };
  })),
  http.patch('/api/v1/system/alerts/preferences', guarded(async (request) => {
    Object.assign(store().alerts, await request.json());
    return store().alerts;
  })),
  http.post('/api/v1/system/alerts/:id/dismiss', guarded((_, { id }) => {
    if (!store().active.some((item) => item.event_id === id)) return error(409, 'alert_not_active');
    store().dismissed.push(id);
    return { dismissed_event_id: id };
  }))
];
