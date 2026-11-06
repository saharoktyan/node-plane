import { setupWorker } from 'msw/browser';
import { i18n } from '$lib/i18n/index.svelte';
import { handlers } from './handlers';
import { choice } from './scenario';

/** Starts the mocks; anything they do not answer is a mistake to see. */
export async function start() {
  i18n.set(choice().lang);
  await setupWorker(...handlers).start({ onUnhandledRequest: 'warn', quiet: true });
}
