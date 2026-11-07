import { describe as group, expect, it } from 'vitest';
import { describe, ordered, type Alert } from './alerts';

const alert = (kind: string, payload: Alert['payload'] = {}, extra: Partial<Alert> = {}): Alert => ({ node_key: 'de1', kind,
  title: 'Frankfurt', first_seen_at: '', last_seen_at: '', event_id: 'e', dismissed: false, payload, ...extra });

group('alerts', () => {
  it('names a resource with its reading, as the bot does', () => {
    expect(describe(alert('disk_low', { value: 7.25 }))).toEqual({ key: 'kind_disk_low', reading: { key: 'reading_disk_low', value: '7.3' } });
    expect(describe(alert('load_high', { value: 3.456, cpus: 2 })).reading).toEqual({ key: 'reading_load_high', value: '3.46', cpus: 2 });
  });

  it('names the rest without a reading', () => {
    expect(describe(alert('node_unreachable', { value: 1 }))).toEqual({ key: 'kind_node_unreachable', reading: null });
    expect(describe(alert('cpu_high'))).toEqual({ key: 'kind_cpu_high', reading: null });
    expect(describe(alert('something_new')).key).toBe('kind_other');
  });

  it('puts alerts already seen last', () => {
    const items = ordered([alert('ram_high', {}, { dismissed: true, title: 'A' }), alert('awg_down', {}, { title: 'B' })]);
    expect(items.map((item) => item.kind)).toEqual(['awg_down', 'ram_high']);
  });
});
