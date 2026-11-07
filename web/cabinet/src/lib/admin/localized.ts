/** Texts written in several languages, as the bot and npw edit them: the
 * source language must have a text; a language without its own shows the
 * one chosen for it, else the source's. The controller checks the same. */
import type { Schemas } from '$lib/api/client';

export type Localized = Schemas['LocalizedTextOutput'];
export type Draft = { source: string; texts: Record<string, string>; fallback: Record<string, string> };

/** What the editor starts from: the stored value, or an empty one. */
export function draftOf(value: Localized | null | undefined, languages: string[], source: string): Draft {
  const texts = Object.fromEntries(languages.map((language) => [language, value?.texts[language] ?? '']));
  return { source: value?.source ?? source, texts, fallback: { ...(value?.fallback ?? {}) } };
}

/** The body the controller takes, or null when the source has no text. */
export function inputOf(draft: Draft, languages: string[]): Schemas['LocalizedTextInput'] | null {
  const texts = Object.fromEntries(languages.map((language) => [language, (draft.texts[language] ?? '').trim()])
    .filter(([, text]) => text));
  if (!texts[draft.source]) return null;
  // Only a language without its own text keeps a fallback, and only to one that has text.
  const fallback = Object.fromEntries(Object.entries(draft.fallback)
    .filter(([language, target]) => !texts[language] && texts[target] && target !== draft.source));
  return { source: draft.source, texts, fallback } as Schemas['LocalizedTextInput'];
}

/** Whether nothing at all is written: an optional text then goes away. */
export const empty = (draft: Draft) => Object.values(draft.texts).every((text) => !text.trim());

/** The text a reader of this language sees. */
export function shownText(value: Localized | null | undefined, language: string): string {
  if (!value) return '';
  return value.texts[language] ?? value.texts[value.fallback[language] ?? value.source] ?? '';
}
