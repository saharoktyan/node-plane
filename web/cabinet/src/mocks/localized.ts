/** Texts in several languages on mocks, as the controller stores them. */
import type { Schemas } from '$lib/api/client';

/** A stored text as the controller gives it back, with what each language shows. */
export function localized(input: Schemas['LocalizedTextInput']): Schemas['LocalizedTextOutput'] {
  const shown = Object.fromEntries(['en', 'ru'].map((language) =>
    [language, input.texts[language] ?? input.texts[input.fallback?.[language] ?? input.source] ?? '']));
  return { source: input.source, texts: { ...input.texts } as Record<string, string>, fallback: { ...input.fallback }, shown };
}
