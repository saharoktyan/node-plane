/** The signed-in account (`GET /api/v1/me`): who it is, its language and
 * what it may do. The API decides every permission; the cabinet only hides
 * what would be refused. */
import { goto } from '$app/navigation';
import { api, call, type Schemas } from '$lib/api/client';
import { i18n } from '$lib/i18n/index.svelte';
import { session } from '$lib/session.svelte';

type Me = Schemas['MeOutput'];

class Account {
  me = $state<Me | null>(null);

  get staff() {
    return this.me?.role === 'admin';
  }

  may(permission: string) {
    return Boolean(this.me?.permissions.includes(permission));
  }

  async load(): Promise<Me> {
    this.me = await call(api.GET('/api/v1/me'));
    i18n.set(this.me.locale);
    return this.me;
  }

  /** The account, or the sign-in page when there is no session. */
  async require(): Promise<Me | null> {
    if (!session.present) {
      await goto('/login?next=' + encodeURIComponent(location.pathname + location.search));
      return null;
    }
    try {
      return this.me ?? (await this.load());
    } catch (error) {
      if (!session.present) {
        await goto('/login');
        return null;
      }
      throw error;
    }
  }

  async signOut() {
    try {
      const current = (await call(api.GET('/api/v1/me/sessions'))).items.find((item) => item.current);
      if (current)
        await api.DELETE('/api/v1/me/sessions/{session_id}', { params: { path: { session_id: current.id } } });
    } finally {
      session.close();
      this.me = null;
      await goto('/login');
    }
  }
}

export const account = new Account();
