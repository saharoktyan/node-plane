/** What waits in the administration's system sections: an update, and
 * alerts nobody has seen yet. A page that changes them asks again. */
import { api, call } from '$lib/api/client';

export const attention = $state<{ update_available: boolean; unacknowledged_alerts: number }>({
  update_available: false, unacknowledged_alerts: 0
});

export async function refreshAttention() {
  try {
    Object.assign(attention, await call(api.GET('/api/v1/system/attention')));
  } catch {
    // The marks stay as they were.
  }
}
