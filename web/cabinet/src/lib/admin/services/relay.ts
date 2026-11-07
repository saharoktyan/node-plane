/** The web cabinet's relay and the cabinet itself, as Workstation edits
 * them: the relay's settings with their checks, and the controller's own
 * cabinet server. */

export const TLS = ['acme', 'files', 'proxy'] as const;
export type Tls = (typeof TLS)[number];
export type RelayDraft = { node_key: string; domain: string; port: string; tls: Tls; acme_contact: string;
  certificate_path: string; key_path: string };
export type Relay = { node_key: string; domain: string; port: number; tls: Tls; acme_contact?: string | null;
  certificate_path?: string | null; key_path?: string | null; status: string; url: string; error_code?: string | null;
  link?: { agent_reachable: boolean; running: boolean; connected: boolean; error?: string | null } | null };

export function relayDraft(relay: Relay | null, node = ''): RelayDraft {
  return { node_key: relay?.node_key ?? node, domain: relay?.domain ?? '', port: String(relay?.port ?? 443),
    tls: relay?.tls ?? 'acme', acme_contact: relay?.acme_contact ?? '', certificate_path: relay?.certificate_path ?? '',
    key_path: relay?.key_path ?? '' };
}

const DOMAIN = /^(?!-)[a-z0-9-]+(?<!-)(\.(?!-)[a-z0-9-]+(?<!-))+$/;

/** An error key for the draft, or the body to send. */
export function relayBody(draft: RelayDraft): { error: string } | { body: Record<string, unknown> } {
  const domain = draft.domain.trim().toLowerCase();
  const port = Number(draft.port.trim());
  if (!draft.node_key) return { error: 'relay_node_needed' };
  if (!domain) return { error: 'relay_domain_needed' };
  if (domain.length > 253 || !DOMAIN.test(domain)) return { error: 'relay_domain_invalid' };
  if (!Number.isInteger(port) || port < 1 || port > 65535) return { error: 'relay_port_invalid' };
  // Let's Encrypt checks the domain on port 443.
  if (draft.tls === 'acme' && port !== 443) return { error: 'relay_acme_port' };
  const contact = draft.acme_contact.trim();
  if (draft.tls === 'acme' && contact && !contact.includes('@')) return { error: 'relay_contact_invalid' };
  const certificate = draft.certificate_path.trim();
  const key = draft.key_path.trim();
  if (draft.tls === 'files' && (!certificate || !key)) return { error: 'relay_files_needed' };
  if (draft.tls === 'files' && (!certificate.startsWith('/') || !key.startsWith('/'))) return { error: 'relay_path' };
  const body: Record<string, unknown> = { node_key: draft.node_key, domain, port, tls: draft.tls };
  if (draft.tls === 'acme' && contact) body.acme_contact = contact;
  if (draft.tls === 'files') Object.assign(body, { certificate_path: certificate, key_path: key });
  return { body };
}

/** The link's state as one key: whether the controller is attached to the relay now. */
export function linkState(relay: Relay): string {
  const link = relay.link;
  if (!link) return 'unchecked';
  if (!link.agent_reachable) return 'agent';
  if (!link.running) return 'stopped';
  return link.connected ? 'connected' : 'disconnected';
}

export const DEFAULT_LISTEN = '0.0.0.0:8090';

/** An error key for the controller's own cabinet server, or its body. */
export function cabinetBody(listen: string, publicUrl: string): { error: string } | { body: Record<string, unknown> } {
  const address = listen.trim();
  const match = /^(\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]):(\d{1,5})$/.exec(address);
  const port = match ? Number(match[2]) : 0;
  if (!match || port < 1 || port > 65535 || port === 8080) return { error: 'cabinet_listen_invalid' };
  const url = publicUrl.trim();
  if (!url) return { error: 'cabinet_url_needed' };
  if (!/^https?:\/\/[^\s/]+/i.test(url) || /\s/.test(url)) return { error: 'cabinet_url_invalid' };
  return { body: { mode: 'local', listen: address, public_url: url } };
}
