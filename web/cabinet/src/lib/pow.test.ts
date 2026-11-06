import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { leadingZeroBits, solve } from './pow';

describe('registration puzzle', () => {
  it('counts leading zero bits', () => {
    expect(leadingZeroBits(new Uint8Array([0, 0x0f, 0xff]))).toBe(12);
    expect(leadingZeroBits(new Uint8Array([0x80]))).toBe(0);
  });

  it('finds a nonce the controller accepts', async () => {
    const nonce = await solve('abc', 8);
    const digest = createHash('sha256').update(`abc:${nonce}`).digest();
    expect(leadingZeroBits(new Uint8Array(digest))).toBeGreaterThanOrEqual(8);
  });
});
