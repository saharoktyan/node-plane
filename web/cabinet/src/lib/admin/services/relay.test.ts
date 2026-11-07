import { describe, expect, it } from 'vitest';
import { cabinetBody, linkState, relayBody, relayDraft, type Relay } from './relay';

describe('relay settings', () => {
  it('checks what is typed as Workstation does', () => {
    const draft = relayDraft(null, 'n1');
    expect(relayBody({ ...draft, domain: '' })).toEqual({ error: 'relay_domain_needed' });
    expect(relayBody({ ...draft, domain: 'nodot' })).toEqual({ error: 'relay_domain_invalid' });
    expect(relayBody({ ...draft, domain: 'cab.example.com', port: '8443' })).toEqual({ error: 'relay_acme_port' });
    expect(relayBody({ ...draft, domain: 'cab.example.com', acme_contact: 'me' })).toEqual({ error: 'relay_contact_invalid' });
    expect(relayBody({ ...draft, node_key: '' , domain: 'cab.example.com'})).toEqual({ error: 'relay_node_needed' });
    expect(relayBody({ ...draft, domain: 'cab.example.com', tls: 'files' })).toEqual({ error: 'relay_files_needed' });
    expect(relayBody({ ...draft, domain: 'cab.example.com', tls: 'files', certificate_path: 'c', key_path: '/k' }))
      .toEqual({ error: 'relay_path' });
  });

  it('sends only what the TLS mode uses', () => {
    const draft = { ...relayDraft(null, 'n1'), domain: 'Cab.Example.com', acme_contact: 'me@example.com',
      certificate_path: '/c', key_path: '/k' };
    expect(relayBody(draft)).toEqual({ body: { node_key: 'n1', domain: 'cab.example.com', port: 443, tls: 'acme',
      acme_contact: 'me@example.com' } });
    expect(relayBody({ ...draft, tls: 'proxy', port: '8443' })).toEqual({ body: { node_key: 'n1',
      domain: 'cab.example.com', port: 8443, tls: 'proxy' } });
  });

  it('names the link', () => {
    const relay = { link: null } as unknown as Relay;
    expect(linkState(relay)).toBe('unchecked');
    expect(linkState({ ...relay, link: { agent_reachable: true, running: true, connected: true } })).toBe('connected');
    expect(linkState({ ...relay, link: { agent_reachable: true, running: false, connected: false } })).toBe('stopped');
  });

  it('checks the controller cabinet server', () => {
    expect(cabinetBody('0.0.0.0:8080', 'https://p.example.com')).toEqual({ error: 'cabinet_listen_invalid' });
    expect(cabinetBody('0.0.0.0:9000', '')).toEqual({ error: 'cabinet_url_needed' });
    expect(cabinetBody('0.0.0.0:9000', 'p.example.com')).toEqual({ error: 'cabinet_url_invalid' });
    expect(cabinetBody('0.0.0.0:9000', 'https://p.example.com')).toEqual({ body: { mode: 'local',
      listen: '0.0.0.0:9000', public_url: 'https://p.example.com' } });
  });
});
