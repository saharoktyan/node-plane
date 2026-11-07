/** A server's settings as the bot and Workstation edit them: the fields of
 * each tab, their checks, and the change sent to the controller with only
 * what was edited. */

export type Tab = 'general' | 'protocols' | 'advanced' | 'awg';
export type FieldKey = (typeof FIELDS)[number][0];

// Key, whether it is a protocol setting (under `settings`), and its value when unset.
export const FIELDS = [
  ['title', false, ''], ['region', false, ''], ['flag', false, ''], ['public_host', true, ''],
  ['ssh_target', false, ''], ['notes', false, ''], ['xray_host', true, ''], ['xray_sni', true, 'www.cloudflare.com'],
  ['xray_tcp_port', true, '443'], ['xray_xhttp_port', true, '8443'], ['xray_xhttp_path', true, '/assets'],
  ['xray_fingerprint', true, 'chrome'], ['awg_public_host', true, ''], ['awg_interface', true, 'wg0'],
  ['awg_port', true, '443'], ['awg_port_mode', true, 'auto'], ['awg_i1_preset', true, 'quic'],
  ['awg_parameters', true, 'auto'], ['ipv6_outbound', true, 'on']
] as const;

export const CHOICES: Partial<Record<FieldKey, string[]>> = {
  awg_i1_preset: ['quic', 'dns', 'chaos'],
  awg_port_mode: ['auto', 'manual'],
  ipv6_outbound: ['on', 'off'],
  xray_fingerprint: ['chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'random', 'randomized']
};

type Source = { key: string; desired_revision: number; transport?: string | null; protocols: string[];
  xray_transports?: string[]; settings?: Record<string, unknown> } & Record<string, unknown>;

export type Editor = { source: Source; values: Record<FieldKey, string>; baseline: Record<FieldKey, string>;
  protocols: string[]; transports: string[] };

const setting = (key: FieldKey) => FIELDS.find(([name]) => name === key)![1];

function text(value: unknown, key: FieldKey) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  if (key === 'awg_parameters' && value && typeof value === 'object' && Object.keys(value).length)
    return Object.entries(value).map(([name, item]) => `${name} = ${item}`).join('\n');
  return '';
}

export function editor(source: Source, defaults: Record<string, unknown> = {}): Editor {
  const values = {} as Record<FieldKey, string>;
  const settings = source.settings ?? {};
  for (const [key, isSetting, fallback] of FIELDS) {
    const own = isSetting ? settings[key] : source[key];
    let value = text(own == null && isSetting ? defaults[key] : own, key) || fallback;
    if (!value && (key === 'xray_host' || key === 'awg_public_host')) value = text(settings.public_host, key);
    values[key] = value;
  }
  return { source, values, baseline: { ...values }, protocols: [...source.protocols],
    transports: [...(source.xray_transports ?? [])] };
}

/** The fields a tab shows for this server. */
export function fieldsOf(state: Editor, tab: Tab): FieldKey[] {
  const awg = state.protocols.includes('awg');
  const xray = state.protocols.includes('xray');
  return FIELDS.map(([key]) => key).filter((key) => {
    if (tab === 'general') return (!setting(key) || key === 'public_host') && (key !== 'ssh_target' || state.source.transport === 'remote');
    if (tab === 'awg') return key === 'awg_parameters' && awg;
    if (tab === 'advanced') {
      if (!setting(key) || key === 'public_host' || key === 'awg_parameters') return false;
      return key === 'ipv6_outbound' || (key.startsWith('xray') ? xray : awg);
    }
    return false;
  });
}

export function parseAwg(input: string): Record<string, string> {
  if (input.trim().toLowerCase() === 'auto') return {};
  const values: Record<string, string> = {};
  for (const line of input.split('\n').map((item) => item.trim()).filter((item) => item && !item.startsWith('#'))) {
    const at = line.indexOf('=');
    if (at < 0) throw new Error('awg_line');
    const [name, value] = [line.slice(0, at).trim(), line.slice(at + 1).trim()];
    if (!value || name in values) throw new Error('awg_duplicate');
    values[name] = value;
  }
  if (!Object.keys(values).length) throw new Error('awg_empty');
  return values;
}

/** An error key for a field's value, or null when it is fine. */
export function check(key: FieldKey, input: string): string | null {
  const value = input.trim();
  const limit = key === 'title' || key === 'region' ? 128 : key === 'flag' ? 16 : key === 'notes' ? 2000
    : key === 'awg_parameters' ? 16384 : 255;
  if ([...value].length > limit) return 'too_long';
  if (key === 'flag' || key === 'notes') return null;
  if (!value) return 'required';
  if (key === 'awg_parameters') {
    try {
      parseAwg(value);
    } catch (error) {
      return (error as Error).message;
    }
  } else if (key.endsWith('_port')) {
    const port = Number(value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return 'port';
  } else if (['public_host', 'xray_host', 'awg_public_host', 'xray_sni'].includes(key)) {
    if (!validHost(value)) return 'host';
    if (key === 'xray_sni' && (!value.includes('.') || IPV4.test(value))) return 'sni';
  } else if (key === 'ssh_target') {
    if (!validTarget(value)) return 'target';
  } else if (key === 'xray_xhttp_path') {
    if (!/^\/[A-Za-z0-9/_~.%+-]*$/.test(value)) return 'path';
  } else if (key === 'awg_interface') {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,14}$/.test(value)) return 'interface';
  }
  if (CHOICES[key] && !CHOICES[key]!.includes(value)) return 'choice';
  return null;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** A DNS name or an IPv4 address, as the controller takes a public address. */
export function validHost(host: string) {
  if (IPV4.test(host)) return true;
  if (!host || host.length > 253 || !/^[\x00-\x7f]*$/.test(host)) return false;
  // A numeric name is not DNS: a malformed IPv4 must not pass as one.
  if (!/[A-Za-z]/.test(host.split('.').at(-1) ?? '')) return false;
  return host.split('.').every((label) => /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label));
}

/** user@host, or user@[IPv6]. */
export function validTarget(target: string) {
  if (!target || target.length > 255) return false;
  const at = target.indexOf('@');
  if (at < 0) return false;
  const [user, host] = [target.slice(0, at), target.slice(at + 1)];
  if (!/^[A-Za-z_][A-Za-z0-9._-]*$/.test(user)) return false;
  if (host.startsWith('[') && host.endsWith(']')) return /^[0-9a-fA-F:.]+$/.test(host.slice(1, -1)) && host.includes(':');
  return validHost(host);
}

const same = (a: string[], b: string[] = []) => a.length === b.length && a.every((item) => b.includes(item));

export function toggle(state: Editor, item: string, transport: boolean) {
  const list = transport ? state.transports : state.protocols;
  const next = list.includes(item) ? list.filter((value) => value !== item) : [...list, item];
  if (transport) state.transports = next;
  else state.protocols = next;
  if (!transport && item === 'xray') {
    if (next.includes('xray') && !state.transports.length)
      state.transports = state.source.xray_transports?.length ? [...state.source.xray_transports] : ['tcp', 'xhttp'];
    if (!next.includes('xray')) state.transports = [];
  }
}

export const dirty = (state: Editor) => FIELDS.some(([key]) => state.values[key] !== state.baseline[key])
  || !same(state.protocols, state.source.protocols) || !same(state.transports, state.source.xray_transports);

/** The change to send, or an error key naming what to fix. */
export function draft(state: Editor): { body: Record<string, unknown> } | { error: string; field?: FieldKey } {
  for (const [key] of FIELDS) {
    if (state.values[key] === state.baseline[key]) continue;
    const error = check(key, state.values[key]);
    if (error) return { error, field: key };
  }
  const body: Record<string, unknown> = {};
  const was = state.source.settings ?? {};
  const settings: Record<string, unknown> = { ...was };
  let changed = false;
  for (const [key, isSetting] of FIELDS) {
    const enabled = isSetting && key !== 'awg_parameters' && (['xray', 'awg'] as const).some((protocol) =>
      key.startsWith(protocol) && state.protocols.includes(protocol) && !state.source.protocols.includes(protocol));
    const value = state.values[key];
    if (value === state.baseline[key] && !enabled) continue;
    if (isSetting) {
      const input = enabled && !value && (key === 'xray_host' || key === 'awg_public_host') ? state.values.public_host : value;
      if (!input.trim()) return { error: 'required', field: key };
      settings[key] = key === 'awg_parameters' ? parseAwg(input) : key.endsWith('_port') ? Number(input.trim()) : input.trim();
      changed = true;
    } else {
      if (['title', 'region', 'ssh_target'].includes(key) && !value.trim()) return { error: 'required', field: key };
      body[key] = value.trim();
    }
  }
  if (changed) {
    // A new public address carries the protocol addresses that followed it.
    const old = was.public_host;
    if (settings.public_host !== old) {
      for (const key of ['xray_host', 'awg_public_host']) {
        if ((was[key] ?? old) === old && (settings[key] ?? old) === old) settings[key] = settings.public_host;
      }
    }
    const port = state.values.awg_port !== state.baseline.awg_port;
    const mode = state.values.awg_port_mode !== state.baseline.awg_port_mode;
    if (port && !mode) settings.awg_port_mode = 'manual';
    if (settings.awg_port_mode === 'auto' && !port && (mode || settings.awg_i1_preset !== was.awg_i1_preset)) delete settings.awg_port;
    body.settings = settings;
  }
  if (!same(state.protocols, state.source.protocols)) body.protocols = state.protocols;
  if (!same(state.transports, state.source.xray_transports)) body.xray_transports = state.transports;
  if (state.protocols.includes('xray') && !state.transports.length) return { error: 'transport' };
  if (!Object.keys(body).length) return { error: 'no_changes' };
  return { body };
}
