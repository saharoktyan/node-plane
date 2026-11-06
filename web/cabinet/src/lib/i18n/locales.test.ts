import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = join(__dirname, '../../../locale');
const languages = readdirSync(root);

function keys(language: string) {
  return Object.fromEntries(readdirSync(join(root, language)).sort().map((file) => [
    file, Object.keys(JSON.parse(readFileSync(join(root, language, file), 'utf8'))).sort()]));
}

describe('locale catalogs', () => {
  it('every language has the same sections and keys', () => {
    const reference = keys('en');
    for (const language of languages) expect(keys(language), language).toEqual(reference);
  });

  it('no text is empty', () => {
    for (const language of languages)
      for (const file of readdirSync(join(root, language)))
        for (const [key, value] of Object.entries(JSON.parse(readFileSync(join(root, language, file), 'utf8'))))
          expect(String(value).trim(), `${language}/${file}: ${key}`).not.toBe('');
  });
});
