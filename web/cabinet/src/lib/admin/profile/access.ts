/** A profile's access as the bot and Workstation edit it: servers given by
 * hand, rules that give every server (or a region's) with some protocols,
 * and exclusions that take a server a rule gives away again. The
 * controller works out the result; this keeps the draft in step with it. */

export type Protocol = 'awg' | 'xray';
export type Grant = { node_key: string; protocol: string };
export type Rule = { scope: 'all' | 'region'; region_id: string | null; protocols: Protocol[] };
export type Policy = { explicit_grants: Grant[]; rules: Rule[]; exclusions: Grant[] };
export type AccessNode = {
  key: string; title: string; flag?: string | null; region?: string | null; region_id?: string | null;
  enabled?: boolean; policy_eligible?: boolean; protocols: string[];
};

export const PROTOCOLS: Protocol[] = ['awg', 'xray'];
const id = (node_key: string, protocol: string) => node_key + '\n' + protocol;
const set = (grants: Grant[]) => new Set(grants.map((g) => id(g.node_key, g.protocol)));
const list = (ids: Set<string>): Grant[] => [...ids].sort().map((item) => {
  const [node_key, protocol] = item.split('\n');
  return { node_key, protocol };
});

/** What the rules give, less the exclusions. */
export function inherited(policy: Policy, nodes: AccessNode[]) {
  const result = new Set<string>();
  for (const node of nodes) {
    if (!(node.policy_eligible ?? node.enabled ?? true)) continue;
    for (const rule of policy.rules) {
      if (rule.scope === 'all' || (node.region_id && node.region_id === rule.region_id)) {
        for (const protocol of rule.protocols) if (node.protocols.includes(protocol)) result.add(id(node.key, protocol));
      }
    }
  }
  for (const item of set(policy.exclusions)) result.delete(item);
  return result;
}

/** Every server and protocol the profile reaches. */
export function effective(policy: Policy, nodes: AccessNode[]) {
  return new Set([...set(policy.explicit_grants), ...inherited(policy, nodes)]);
}

export const has = (grants: Set<string>, node_key: string, protocol: string) => grants.has(id(node_key, protocol));

/** Give or take one server's protocol: a rule's grant is taken by an
 * exclusion, anything else by hand. */
export function toggle(policy: Policy, nodes: AccessNode[], node_key: string, protocol: string, add: boolean): Policy {
  const manual = set(policy.explicit_grants);
  const excluded = set(policy.exclusions);
  const target = id(node_key, protocol);
  if (add) {
    excluded.delete(target);
    if (!inherited({ ...policy, exclusions: list(excluded) }, nodes).has(target)) manual.add(target);
  } else {
    manual.delete(target);
    if (inherited({ ...policy, exclusions: [] }, nodes).has(target)) excluded.add(target);
  }
  return { ...policy, explicit_grants: list(manual), exclusions: list(excluded) };
}

/** Give or take every protocol of these servers. */
export function bulk(policy: Policy, nodes: AccessNode[], scoped: AccessNode[], add: boolean): Policy {
  let next = policy;
  for (const node of scoped) {
    if (add && node.enabled === false) continue;
    for (const protocol of node.protocols) next = toggle(next, nodes, node.key, protocol, add);
  }
  return next;
}

/** Turn a protocol of a rule on or off; a rule left with none goes. */
export function rule(policy: Policy, scope: 'all' | 'region', region_id: string | null, protocol: Protocol, on: boolean): Policy {
  const same = (item: Rule) => item.scope === scope && (item.region_id ?? null) === region_id;
  const current = policy.rules.find(same);
  const protocols = PROTOCOLS.filter((p) => (p === protocol ? on : current?.protocols.includes(p) ?? false));
  const rules = policy.rules.filter((item) => !same(item));
  if (protocols.length) rules.push({ scope, region_id, protocols });
  rules.sort((a, b) => a.scope.localeCompare(b.scope) || (a.region_id ?? '').localeCompare(b.region_id ?? ''));
  return { ...policy, rules };
}

export function same(a: Policy, b: Policy) {
  const norm = (policy: Policy) => JSON.stringify([list(set(policy.explicit_grants)), list(set(policy.exclusions)),
    [...policy.rules].map((r) => [r.scope, r.region_id ?? '', [...r.protocols].sort()]).sort()]);
  return norm(a) === norm(b);
}
