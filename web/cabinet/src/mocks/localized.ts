/** Texts in several languages on mocks, as the controller stores them. */
import type { Schemas } from '$lib/api/client';

/** A stored text as the controller gives it to an editor: each language with whose text it shows. */
export function localized(input: Schemas['LocalizedTextInput']): Schemas['LocalizedTextOutput'] {
  const texts = { ...input.texts } as Record<string, string>;
  const shown = Object.fromEntries(['en', 'ru'].map((language) =>
    [language, texts[language] ? language : (input.fallback?.[language] ?? input.source)]));
  return { source: input.source, texts, fallback: { ...input.fallback }, shown };
}

/** The text a reader of this language gets. */
export const textIn = (value: Schemas['LocalizedTextOutput'] | null | undefined, language: string) =>
  value ? (value.texts[value.shown[language] ?? value.source] ?? '') : '';
