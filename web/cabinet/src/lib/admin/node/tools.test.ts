import { describe, expect, it } from 'vitest';
import type { Node } from '$lib/admin/nodes';
import { actions, byStatus, checkName, recoveryPath } from './tools';

const node = (extra: Partial<Node> = {}) => ({ key: 'lv1', title: 'Riga', region: 'EU', flag: '', enabled: true,
  protocols: ['awg', 'xray'], desired_revision: 2, applied_revision: 2, notes: '', policy_eligible: true,
  service_instances: [], setup_attempted: true, ...extra }) as Node;

describe('node maintenance', () => {
  it('offers nothing before the VPN is installed', () => {
    expect(actions(node({ applied_revision: 0 }), { docker: true })).toEqual([]);
    expect(actions(node({ protocols: [] }), { docker: true })).toEqual([]);
    expect(actions(node(), null)).toEqual([]);
  });

  it('keeps configurations only when every protocol has a valid one', () => {
    const install = (services: Record<string, unknown>) => actions(node(), services)[0][1];
    expect(install({ docker: true, awg_config_valid: true, xray_config_valid: true })).toEqual(['reinstall_keep', 'reinstall_clean']);
    expect(install({ docker: true, awg_config_valid: true })).toEqual(['reinstall_clean']);
    expect(install({ docker: false })).toEqual(['install_docker', 'reinstall_clean']);
  });

  it('repairs only what the server carries', () => {
    const repair = actions(node({ protocols: ['awg'] }), { docker: true, runtime_drift: true })[2][1];
    expect(repair).toEqual(['sync_env', 'sync_runtime', 'reconcile_access', 'regenerate_entropy']);
  });

  it('names checks with their protocol and keeps new ones readable', () => {
    expect(checkName('awg:running')).toEqual({ key: 'check_running', protocol: 'awg' });
    expect(checkName('load1')).toEqual({ key: 'check_load' });
    expect(checkName('some_new_check')).toEqual({ raw: 'some new check' });
    expect(byStatus([{ status: 'ok' }, { status: 'error' }, { status: 'warning' }]).map((c) => c.status))
      .toEqual(['error', 'warning', 'ok']);
  });

  it('sends recovery where Workstation does', () => {
    expect(recoveryPath('bootstrap', 'b1', 'cancel')).toBe('/api/v1/system/recovery/bootstrap/b1/cancel');
    expect(recoveryPath('settings', 's1', 'force')).toBe('/api/v1/system/recovery/settings/s1/force');
    expect(recoveryPath('removal', 'lv1', 'resume')).toBe('/api/v1/system/recovery/removal/lv1/resume');
    expect(recoveryPath('agent', 'a1', 'resolve')).toBeNull();
  });
});
