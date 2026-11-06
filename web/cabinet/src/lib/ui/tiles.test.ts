import { describe, expect, it } from 'vitest';
import { columns } from './tiles';

describe('tile columns', () => {
  it('keeps one line while the tiles fit', () => expect(columns(1200, 4)).toBe(4));
  it('makes four two by two rather than three and one', () => expect(columns(560, 4)).toBe(2));
  it('keeps two columns on a phone', () => expect(columns(358, 4)).toBe(2));
  it('makes five three and two', () => expect(columns(600, 5)).toBe(3));
  it('stacks on a narrow screen', () => expect(columns(140, 4)).toBe(1));
});
