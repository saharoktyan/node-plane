/** The controller's registration puzzle: a nonce such that
 * sha256("challenge:nonce") starts with `bits` zero bits. */
export function leadingZeroBits(digest: Uint8Array): number {
  let bits = 0;
  for (const byte of digest) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    return bits + Math.clz32(byte) - 24;
  }
  return bits;
}

export async function solve(challenge: string, bits: number, onProgress?: (tried: number) => void): Promise<string> {
  const encoder = new TextEncoder();
  for (let nonce = 0; ; nonce += 1) {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(`${challenge}:${nonce}`)));
    if (leadingZeroBits(digest) >= bits) return String(nonce);
    if (nonce % 5000 === 0) onProgress?.(nonce);
  }
}
