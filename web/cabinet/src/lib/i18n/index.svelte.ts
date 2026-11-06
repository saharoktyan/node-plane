/** The cabinet's languages: `locale/<language>/<section>.json`, one file per
 * section of the app, the same keys in every language (`npm test` checks).
 * A key is `section.name`: `t('services.request')` reads `request` from
 * `locale/<language>/services.json`. */
const files = import.meta.glob('/locale/*/*.json', { eager: true, import: 'default' }) as Record<
  string,
  Record<string, string>
>;

export const LANGUAGES = ['ru', 'en'] as const;
export type Language = (typeof LANGUAGES)[number];

export const catalogs: Record<string, Record<string, string>> = {};
for (const [path, entries] of Object.entries(files)) {
  const [, , language, file] = path.split('/');
  const section = file.replace(/\.json$/, '');
  const catalog = (catalogs[language] ??= {});
  for (const [key, value] of Object.entries(entries)) catalog[`${section}.${key}`] = value;
}

function preferred(): Language {
  const asked = typeof navigator === 'undefined' ? 'ru' : navigator.language.slice(0, 2);
  return (LANGUAGES as readonly string[]).includes(asked) ? (asked as Language) : 'en';
}

class I18n {
  language = $state<Language>(preferred());

  set(value: string | null | undefined) {
    if (value && (LANGUAGES as readonly string[]).includes(value)) this.language = value as Language;
    if (typeof document !== 'undefined') document.documentElement.lang = this.language;
  }
}

export const i18n = new I18n();

/** The text for a key in the current language, with `{name}` filled in. */
export function t(key: string, values: Record<string, string | number> = {}): string {
  const text = catalogs[i18n.language]?.[key] ?? catalogs.en?.[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (name in values ? String(values[name]) : whole));
}
