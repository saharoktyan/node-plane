/** A dangerous action asks the cabinet session to confirm itself again with
 * its password or a passkey; the controller says so with
 * `confirmation_required`. The action is then tried once more. */
import { ApiError } from '$lib/api/client';

export const confirmation = $state<{ open: boolean; done: ((confirmed: boolean) => void) | null }>({ open: false, done: null });

/** Opens the question and waits for its answer. */
function ask(): Promise<boolean> {
  return new Promise((resolve) => {
    confirmation.done = resolve;
    confirmation.open = true;
  });
}

/** Closes the question with its answer. */
export function answer(confirmed: boolean) {
  const done = confirmation.done;
  confirmation.done = null;
  confirmation.open = false;
  done?.(confirmed);
}

/** Runs the action, confirming the session first if the controller asks. A
 * refused confirmation leaves the controller's error to the caller. */
export async function confirmed<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== 'confirmation_required' || !(await ask())) throw error;
    return work();
  }
}
