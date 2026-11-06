/** A row of tiles that wraps without leaving empty cells: all in one line
 * when they fit; otherwise as even a grid as the width allows (four become
 * two by two, not three and one); a shorter last line stretches its tiles to
 * the full width (five become three and two). */
export function columns(width: number, count: number, min = 150, gap = 1): number {
  if (count < 1) return 1;
  const fit = Math.max(1, Math.floor((width + gap) / (min + gap)));
  if (fit >= count) return count;
  return Math.ceil(count / Math.ceil(count / fit));
}

/** Sets `--columns` on a `.stats` row as its width or tiles change. */
export function tiles(node: HTMLElement) {
  const update = () => node.style.setProperty('--columns', String(columns(node.clientWidth, node.children.length)));
  const resize = new ResizeObserver(update);
  const mutation = new MutationObserver(update);
  resize.observe(node);
  mutation.observe(node, { childList: true });
  update();
  return {
    destroy() {
      resize.disconnect();
      mutation.disconnect();
    }
  };
}
