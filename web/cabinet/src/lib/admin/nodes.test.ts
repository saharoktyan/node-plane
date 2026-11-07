import { describe, expect, it } from 'vitest';
import { agentOf, needsSetup, pendingSettings, protocolStatus, stateKey, type Node, type Overview } from './nodes';

const node = (extra: Partial<Node> = {}) => ({ key: 'lv1', title: 'Riga', region: 'EU', flag: '', enabled: true,
  protocols: ['awg'], desired_revision: 1, applied_revision: 0, notes: '', policy_eligible: true,
  service_instances: [], setup_attempted: false, ...extra }) as Node;

describe('node lines', () => {
  it('names a server without its agent an invitation', () => {
    expect(stateKey(node({ stage: 'invitation' }))).toBe('invitation');
    expect(stateKey(node({ stage: 'node' }))).toBe('not_installed');
    expect(stateKey(node({ stage: 'node', applied_revision: 1, desired_revision: 2 }))).toBe('changes_pending');
  });

  it('reads the agent from the services error', () => {
    expect(agentOf(null)).toBe('ready');
    expect(agentOf('node_agent_unconfigured')).toBe('missing');
    expect(agentOf('something')).toBe('unknown');
  });

  it('offers setup once the settings are complete', () => {
    const overview = { state: 'not_installed', settings_complete: true } as Overview;
    expect(needsSetup(node(), overview, 'ready', null)).toBe(true);
    expect(needsSetup(node(), { ...overview, settings_complete: false }, 'missing', null)).toBe(false);
  });

  it('tells a protocol\'s state from the services', () => {
    expect(protocolStatus(node(), null, 'awg')).toBe('unchecked');
    expect(protocolStatus(node(), { awg_running: true }, 'awg')).toBe('running');
    expect(protocolStatus(node(), { docker: false }, 'awg')).toBe('unavailable');
    expect(protocolStatus(node({ applied_revision: 1 }), {}, 'xray')).toBe('missing');
  });

  it('waits for settings to apply on an installed node', () => {
    expect(pendingSettings(node({ applied_revision: 1, desired_revision: 2 }))).toBe(true);
    expect(pendingSettings(node({ applied_revision: 1, desired_revision: 2, settings_apply_pending: false }))).toBe(false);
  });
});
