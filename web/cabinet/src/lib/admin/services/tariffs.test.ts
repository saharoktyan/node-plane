import { describe, expect, it } from 'vitest';
import { accessLabel, accessOf, chosenRegions, everyNode, keepsNodes } from './tariffs';

describe('tariff nodes', () => {
  it('keeps the profile nodes without rules', () => {
    expect(accessOf('keep')).toBeNull();
    expect(keepsNodes(null)).toBe(true);
    expect(keepsNodes({})).toBe(true);
    expect(accessLabel({}, [])).toEqual({ key: 'keep_nodes' });
    // No region chosen is no rule, so the profile's nodes stay.
    expect(accessOf('regions', [])).toBeNull();
  });

  it('grants every node or the regions chosen', () => {
    const all = accessOf('all');
    expect(everyNode(all)).toBe(true);
    expect(accessLabel(all, [])).toEqual({ key: 'all_nodes' });
    const some = accessOf('regions', ['r1', 'r2']);
    expect(everyNode(some)).toBe(false);
    expect(chosenRegions(some)).toEqual(['r1', 'r2']);
    expect(accessLabel(some, [{ id: 'r1', title: 'Европа' }])).toEqual({ names: 'Европа, ?' });
  });
});
