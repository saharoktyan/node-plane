import { describe, expect, it } from 'vitest';
import { generic, moduleOf, serviceName } from './registry';

describe('service registry', () => {
  it('VPN has its own module, other types the generic one', () => {
    expect(moduleOf('vpn')).not.toBe(generic);
    expect(moduleOf('vaultwarden')).toBe(generic);
  });

  it('names known types and falls back to the id', () => {
    expect(serviceName('vpn')).toBe('VPN');
    expect(serviceName('vaultwarden')).toBe('vaultwarden');
  });
});
