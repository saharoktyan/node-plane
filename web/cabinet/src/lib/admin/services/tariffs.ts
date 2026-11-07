/** Tariffs as the bot and Workstation keep them: a term, the nodes a tariff
 * grants and its own limits. Its term and nodes are copied to a profile when
 * the tariff is assigned or renewed; its limits apply while it is assigned. */
import type { Schemas } from '$lib/api/client';

export type Tariff = Schemas['TariffOutput'];
export type Access = Schemas['GrantPolicyInput'] | null;
export type Region = { id: string; title: string };

/** Access terms offered in days; none means the term stays as it is. */
export const TERMS = [7, 30, 90, 180, 365] as const;

type Rule = { scope?: unknown; region_id?: unknown };
const rules = (policy: unknown): Rule[] =>
  policy && typeof policy === 'object' && Array.isArray((policy as { rules?: unknown }).rules)
    ? ((policy as { rules: Rule[] }).rules) : [];

// An empty policy keeps the profile's own nodes, as the controller reads it.
export const keepsNodes = (policy: unknown) => !rules(policy).length;
export const everyNode = (policy: unknown) => rules(policy).some((rule) => rule.scope === 'all');
export const chosenRegions = (policy: unknown) => rules(policy)
  .filter((rule) => rule.scope === 'region' && typeof rule.region_id === 'string').map((rule) => rule.region_id as string);

const grant = (rules: Schemas['GrantPolicyRuleInput'][]): Access =>
  rules.length ? { explicit_grants: [], exclusions: [], rules } : null;

/** The policy for keeping the profile's nodes, every node, or the regions chosen. */
export function accessOf(mode: 'keep' | 'all' | 'regions', regions: string[] = []): Access {
  if (mode === 'all') return grant([{ scope: 'all', region_id: null, protocols: ['awg', 'xray'] }]);
  if (mode === 'regions') return grant(regions.map((id) => ({ scope: 'region', region_id: id, protocols: ['awg', 'xray'] })));
  return null;
}

/** A tariff's nodes in one line: a key to translate, or the regions' names. */
export function accessLabel(policy: unknown, regions: Region[]): { key: string } | { names: string } {
  if (keepsNodes(policy)) return { key: 'keep_nodes' };
  if (everyNode(policy)) return { key: 'all_nodes' };
  return { names: chosenRegions(policy).map((id) => regions.find((region) => region.id === id)?.title ?? '?').join(', ') };
}
