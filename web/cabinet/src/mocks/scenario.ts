/** What the mocks show, chosen in the address and kept for the tab:
 * `?scenario=default|empty|many|errors&role=owner|support|operator|member&lang=ru|en`.
 * `?scenario=reset` returns to the defaults. */
export type Scenario = 'default' | 'empty' | 'many' | 'errors';
export type Role = 'owner' | 'admin' | 'operator' | 'support' | 'member';

const KEY = 'node-plane.mock';
type Choice = { scenario: Scenario; role: Role; lang: 'ru' | 'en' };
const DEFAULT: Choice = { scenario: 'default', role: 'owner', lang: 'ru' };

export function choice(): Choice {
  let saved: Partial<Choice> = {};
  try {
    saved = JSON.parse(sessionStorage.getItem(KEY) ?? '{}');
  } catch {
    saved = {};
  }
  const asked = new URLSearchParams(location.search);
  if (asked.get('scenario') === 'reset') saved = {};
  const value: Choice = {
    scenario: (asked.get('scenario') as Scenario) ?? saved.scenario ?? DEFAULT.scenario,
    role: (asked.get('role') as Role) ?? saved.role ?? DEFAULT.role,
    lang: (asked.get('lang') as 'ru' | 'en') ?? saved.lang ?? DEFAULT.lang
  };
  if (value.scenario === ('reset' as Scenario)) value.scenario = DEFAULT.scenario;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    // Without storage every page starts from the address.
  }
  return value;
}
