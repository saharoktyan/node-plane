/** How things appear and go: a block unfolds to its height and fades in,
 * and folds back the same way. Without motion it only fades, briefly. */
import { cubicInOut } from 'svelte/easing';
import type { TransitionConfig } from 'svelte/transition';

const still = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

export function unfold(node: Element, { duration = 300 }: { duration?: number } = {}): TransitionConfig {
  if (still()) return { duration: 120, css: (t) => `opacity: ${t}` };
  // Its children's margins stay inside it, clipped or not, so the height
  // it eases to is the height it keeps: the end does not jump.
  if (getComputedStyle(node).display === 'block') (node as HTMLElement).style.display = 'flow-root';
  const style = getComputedStyle(node);
  const height = parseFloat(style.height);
  const [top, bottom] = [parseFloat(style.paddingTop), parseFloat(style.paddingBottom)];
  const [marginTop, marginBottom] = [parseFloat(style.marginTop), parseFloat(style.marginBottom)];
  return {
    duration,
    // Even at both ends, so folding does not rush its last frames.
    easing: cubicInOut,
    css: (t) => `overflow: hidden; opacity: ${Math.min(1, t * 1.6)}; height: ${t * height}px;`
      + `padding-top: ${t * top}px; padding-bottom: ${t * bottom}px;`
      + `margin-top: ${t * marginTop}px; margin-bottom: ${t * marginBottom}px;`
  };
}
