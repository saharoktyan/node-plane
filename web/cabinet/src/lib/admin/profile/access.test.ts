import { describe, expect, it } from 'vitest';
import { bulk, effective, has, rule, same, toggle, type AccessNode, type Policy } from './access';

const nodes: AccessNode[] = [
  { key: 'de1', title: 'Frankfurt', region_id: 'eu', protocols: ['awg', 'xray'] },
  { key: 'nl1', title: 'Amsterdam', region_id: 'eu', protocols: ['xray'] },
  { key: 'us1', title: 'Ashburn', region_id: 'us', protocols: ['awg', 'xray'] },
  { key: 'sg1', title: 'Singapore', region_id: 'as', protocols: ['awg'], policy_eligible: false }
];
const empty: Policy = { explicit_grants: [], rules: [], exclusions: [] };

describe('profile access', () => {
  it('gives what a rule covers, on servers that take it', () => {
    const policy = rule(empty, 'region', 'eu', 'xray', true);
    expect([...effective(policy, nodes)].length).toBe(2);
    expect(has(effective(rule(empty, 'all', null, 'awg', true), nodes), 'sg1', 'awg')).toBe(false);
  });

  it('takes a rule\'s server by an exclusion and gives it back without a hand grant', () => {
    const ruled = rule(empty, 'all', null, 'xray', true);
    const taken = toggle(ruled, nodes, 'de1', 'xray', false);
    expect(taken.exclusions).toEqual([{ node_key: 'de1', protocol: 'xray' }]);
    expect(has(effective(taken, nodes), 'de1', 'xray')).toBe(false);
    const back = toggle(taken, nodes, 'de1', 'xray', true);
    expect(back).toEqual({ ...ruled, exclusions: [], explicit_grants: [] });
  });

  it('gives a server no rule covers by hand', () => {
    const given = toggle(empty, nodes, 'us1', 'awg', true);
    expect(given.explicit_grants).toEqual([{ node_key: 'us1', protocol: 'awg' }]);
    expect(same(toggle(given, nodes, 'us1', 'awg', false), empty)).toBe(true);
  });

  it('gives and takes a region at once', () => {
    const eu = nodes.filter((node) => node.region_id === 'eu');
    const given = bulk(empty, nodes, eu, true);
    expect(effective(given, nodes).size).toBe(3);
    expect(effective(bulk(given, nodes, eu, false), nodes).size).toBe(0);
  });

  it('drops a rule left without protocols', () => {
    const on = rule(empty, 'region', 'eu', 'awg', true);
    expect(rule(on, 'region', 'eu', 'awg', false).rules).toEqual([]);
  });
});
