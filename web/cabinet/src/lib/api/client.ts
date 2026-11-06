/** The controller's API, typed from the contract (`api/openapi.json`,
 * `npm run gen:api`). Every request carries the session's access token;
 * an expired one is refreshed once and the request repeated. */
import createClient from 'openapi-fetch';
import type { components, paths } from './schema';
import { session } from '$lib/session.svelte';

export type Schemas = components['schemas'];

/** An error in the contract's shape: `{"error": {"code", "message"}}`. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string
  ) {
    super(message);
  }
}

// Paths where a 401 is the answer itself, not an expired access token.
const NO_REFRESH = ['/api/v1/sessions', '/api/v1/sessions/refresh', '/api/v1/sessions/passkey',
  '/api/v1/sessions/telegram'];

async function authorized(request: Request): Promise<Response> {
  const attempt = () => {
    const copy = request.clone();
    if (session.access) copy.headers.set('Authorization', 'Bearer ' + session.access);
    return fetch(copy);
  };
  const response = await attempt();
  if (response.status !== 401 || NO_REFRESH.includes(new URL(request.url).pathname)) return response;
  return (await session.refresh()) ? attempt() : response;
}

export const api = createClient<paths>({ baseUrl: '', fetch: authorized });

export async function errorOf(response: Response, body: unknown): Promise<ApiError> {
  const error = (body as { error?: { code?: string; message?: string } } | undefined)?.error;
  return new ApiError(response.status, error?.code ?? 'http_' + response.status, error?.message ?? response.statusText);
}

/** The answer's data, or the API's error thrown. */
export async function call<T>(request: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let result;
  try {
    result = await request;
  } catch {
    throw new ApiError(0, 'network_error', 'The server is unreachable');
  }
  if (result.error !== undefined || !result.response.ok) throw await errorOf(result.response, result.error);
  return result.data as T;
}
