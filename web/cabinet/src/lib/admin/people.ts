/** What the People pages share: how a person is named and the roles staff
 * can hold, as the bot and Workstation show them. */
export const STAFF_ROLES = ['owner', 'admin', 'operator', 'support'] as const;

type Named = {
  login?: string | null;
  display_name?: string | null;
  username?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  telegram_user_id?: number | null;
  id?: string;
};

export function personName(item: Named | null | undefined): string {
  if (!item) return '—';
  const full = [item.first_name, item.last_name].filter(Boolean).join(' ');
  return item.display_name || item.login || (item.username ? '@' + item.username : '') || full
    || (item.telegram_user_id ? String(item.telegram_user_id) : (item.id ?? '').slice(0, 8)) || '—';
}

export function telegramOf(item: Named): string {
  return item.username ? '@' + item.username : item.telegram_user_id ? String(item.telegram_user_id) : '—';
}

/** What a new person is added by: a Telegram ID or @username, else a name. */
export function provisionBody(identity: string): { identity?: string; display_name?: string } {
  const value = identity.trim();
  return value.startsWith('@') || /^\d+$/.test(value) ? { identity: value } : { display_name: value };
}
