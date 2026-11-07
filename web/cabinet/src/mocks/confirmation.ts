/** Confirming the session again on mocks: the password is `secret`, and it
 * holds for five minutes, as on the controller. */
import { http } from 'msw';
import { error, guarded } from './people';

let until = 0;

/** The controller's refusal while the session is not confirmed, else null. */
export const unconfirmed = () => (Date.now() < until ? null : error(403, 'confirmation_required'));
const status = () => ({ required: true, confirmed_until: Date.now() < until ? new Date(until).toISOString() : null,
  password: true, passkeys: false });

export const confirmationHandlers = [
  http.get('/api/v1/me/confirmation', guarded(status)),
  http.post('/api/v1/me/confirmation', guarded(async (request) => {
    const { password } = (await request.json()) as { password: string };
    if (password !== 'secret') return error(403, 'password_incorrect');
    until = Date.now() + 5 * 60_000;
    return status();
  }))
];
