import { describe, expect, it } from 'vitest';
import { check, draft, editor, fieldsOf, parseAwg, toggle } from './editor';

const source = () => ({ key: 'lv1', desired_revision: 3, title: 'Riga', region: 'Europe', flag: '🇱🇻', notes: '',
  transport: 'remote', ssh_target: 'root@lv1', protocols: ['awg'], xray_transports: [],
  settings: { public_host: 'lv1.example.net', awg_port: 443, awg_port_mode: 'auto', awg_i1_preset: 'quic' } });

describe('node settings', () => {
  it('sends only what changed', () => {
    const state = editor(source());
    state.values.title = 'Riga 2';
    expect(draft(state)).toEqual({ body: { title: 'Riga 2' } });
    expect(draft(editor(source()))).toEqual({ error: 'no_changes' });
  });

  it('moves the protocol addresses with the public address', () => {
    const state = editor(source());
    state.values.public_host = 'new.example.net';
    const result = draft(state) as { body: { settings: Record<string, unknown> } };
    expect(result.body.settings.awg_public_host).toBe('new.example.net');
    expect(result.body.settings.xray_host).toBe('new.example.net');
  });

  it('turns the AWG port manual when it is set by hand', () => {
    const state = editor(source());
    state.values.awg_port = '51820';
    const result = draft(state) as { body: { settings: Record<string, unknown> } };
    expect(result.body.settings).toMatchObject({ awg_port: 51820, awg_port_mode: 'manual' });
  });

  it('adds VLESS with its transports and settings', () => {
    const state = editor(source());
    toggle(state, 'xray', false);
    const result = draft(state) as { body: Record<string, unknown> };
    expect(result.body.protocols).toEqual(['awg', 'xray']);
    expect(result.body.xray_transports).toEqual(['tcp', 'xhttp']);
    expect((result.body.settings as Record<string, unknown>).xray_sni).toBe('www.cloudflare.com');
    expect(fieldsOf(state, 'advanced')).toContain('xray_sni');
  });

  it('checks fields and AWG parameters', () => {
    expect(check('awg_port', '70000')).toBe('port');
    expect(check('xray_xhttp_path', 'assets')).toBe('path');
    expect(check('title', '')).toBe('required');
    expect(check('public_host', '10.0.0.300')).toBe('host');
    expect(check('public_host', 'vpn.example.net')).toBeNull();
    expect(check('xray_sni', '1.2.3.4')).toBe('sni');
    expect(check('ssh_target', 'root@lv1.example.net')).toBeNull();
    expect(check('ssh_target', 'lv1.example.net')).toBe('target');
    expect(check('awg_interface', '1wg')).toBe('interface');
    expect(parseAwg('auto')).toEqual({});
    expect(parseAwg('Jc = 4\nJmin = 10')).toEqual({ Jc: '4', Jmin: '10' });
    expect(() => parseAwg('Jc 4')).toThrow();
  });
});
