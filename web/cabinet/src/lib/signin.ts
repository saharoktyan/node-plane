import { goto } from '$app/navigation';
import type { Schemas } from '$lib/api/client';
import { account } from '$lib/account.svelte';
import { session } from '$lib/session.svelte';

/** After any way in: keep the tokens, read the account, go where asked. */
export async function enter(tokens: Schemas['SessionTokens'], next?: string | null) {
  session.open(tokens);
  account.me = null;
  await account.load();
  await goto(next && next.startsWith('/') && !next.startsWith('//') ? next : '/');
}

export function device(): string {
  return navigator.userAgent.slice(0, 256);
}
