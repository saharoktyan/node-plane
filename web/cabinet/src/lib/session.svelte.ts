/** The signed-in session. The access token lives in memory; the refresh
 * token is kept in the browser so a reload stays signed in. Refreshing
 * rotates both; a refresh token used twice ends the session on the
 * controller. */
import type { components } from './api/schema';

type Tokens = components['schemas']['SessionTokens'];
const KEY = 'node-plane.refresh';

function stored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function store(value: string | null) {
  try {
    if (value) localStorage.setItem(KEY, value);
    else localStorage.removeItem(KEY);
  } catch {
    // Without storage the session lasts until the page closes.
  }
}

class Session {
  access = $state<string | null>(null);
  refreshToken = $state<string | null>(stored());
  #pending: Promise<boolean> | null = null;

  get present() {
    return Boolean(this.access || this.refreshToken);
  }

  open(tokens: Tokens) {
    this.access = tokens.access_token;
    this.refreshToken = tokens.refresh_token;
    store(tokens.refresh_token);
  }

  close() {
    this.access = null;
    this.refreshToken = null;
    store(null);
  }

  /** One refresh at a time; false when the session is over. */
  refresh(): Promise<boolean> {
    this.#pending ??= this.#refresh().finally(() => (this.#pending = null));
    return this.#pending;
  }

  async #refresh(): Promise<boolean> {
    if (!this.refreshToken) return false;
    try {
      const response = await fetch('/api/v1/sessions/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: this.refreshToken })
      });
      if (!response.ok) {
        if (response.status === 401) this.close();
        return false;
      }
      this.open(await response.json());
      return true;
    } catch {
      return false;
    }
  }
}

export const session = new Session();
