/** Passkeys in the browser. The controller sends WebAuthn options as JSON
 * (byte fields in base64url) and takes the credential back as JSON. */
function decode(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=');
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0)).buffer;
}

function encode(value: ArrayBuffer | null): string | null {
  if (!value) return null;
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

type Json = Record<string, unknown>;
type Descriptor = { id: string; type: string; transports?: string[] };

export function available(): boolean {
  return typeof window !== 'undefined' && 'PublicKeyCredential' in window;
}

/** Sign in: the options of `POST /api/v1/sessions/passkey/options`. */
export async function get(options: Json): Promise<Json> {
  const publicKey = {
    ...options,
    challenge: decode(options.challenge as string),
    allowCredentials: ((options.allowCredentials as Descriptor[]) ?? []).map((item) => ({ ...item,
      id: decode(item.id) }))
  } as PublicKeyCredentialRequestOptions;
  const credential = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential;
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    response: { clientDataJSON: encode(response.clientDataJSON), authenticatorData: encode(response.authenticatorData),
      signature: encode(response.signature), userHandle: encode(response.userHandle) },
    clientExtensionResults: credential.getClientExtensionResults()
  };
}

/** Registration: the options of `POST /api/v1/me/passkeys/options`. */
export async function create(options: Json): Promise<Json> {
  const user = options.user as Json;
  const publicKey = {
    ...options,
    challenge: decode(options.challenge as string),
    user: { ...user, id: decode(user.id as string) },
    excludeCredentials: ((options.excludeCredentials as Descriptor[]) ?? []).map((item) => ({ ...item,
      id: decode(item.id) }))
  } as PublicKeyCredentialCreationOptions;
  const credential = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential;
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    response: { clientDataJSON: encode(response.clientDataJSON), attestationObject: encode(response.attestationObject),
      transports: response.getTransports?.() ?? [] },
    clientExtensionResults: credential.getClientExtensionResults()
  };
}
