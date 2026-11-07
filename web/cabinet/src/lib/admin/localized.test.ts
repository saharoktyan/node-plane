import { describe, expect, it } from 'vitest';
import { draftOf, empty, inputOf, shownText } from './localized';

const languages = ['en', 'ru'];

describe('texts in languages', () => {
  it('needs the source text and drops empty ones', () => {
    expect(inputOf({ source: 'ru', texts: { ru: '  Месяц ', en: '' }, fallback: {} }, languages))
      .toEqual({ source: 'ru', texts: { ru: 'Месяц' }, fallback: {} });
    expect(inputOf({ source: 'en', texts: { ru: 'Месяц', en: ' ' }, fallback: {} }, languages)).toBeNull();
  });

  it('keeps a fallback only for a language without text, to one with text', () => {
    const draft = { source: 'en', texts: { en: 'Month', ru: '' }, fallback: { ru: 'en' } };
    expect(inputOf(draft, languages)?.fallback).toEqual({});
    expect(inputOf({ ...draft, texts: { en: 'Month', ru: 'Месяц' } }, languages)?.fallback).toEqual({});
  });

  it('starts from the stored value and shows the right text', () => {
    const value = { source: 'ru', texts: { ru: 'Месяц' }, fallback: {}, shown: { en: 'ru', ru: 'ru' } };
    expect(draftOf(value, languages, 'en')).toEqual({ source: 'ru', texts: { en: '', ru: 'Месяц' }, fallback: {} });
    expect(draftOf(null, languages, 'en').source).toBe('en');
    expect(shownText(value, 'en')).toBe('Месяц');
    expect(empty(draftOf(null, languages, 'en'))).toBe(true);
  });
});
