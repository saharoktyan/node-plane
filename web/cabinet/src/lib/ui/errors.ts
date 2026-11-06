import type { ApiError } from '$lib/api/client';
import { t } from '$lib/i18n/index.svelte';

/** The API's error code in words, when the catalog knows it. */
export function errorText(error: ApiError): string {
  const key = 'errors.' + error.code;
  const text = t(key);
  return text === key ? t('errors.unknown', { code: error.code }) : text;
}
