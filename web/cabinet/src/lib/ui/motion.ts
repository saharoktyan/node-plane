/** How things appear and go: a block unfolds to its height and fades in,
 * and folds back the same way. Without motion it only fades, briefly. */
import { cubicOut } from 'svelte/easing';
import type { TransitionConfig } from 'svelte/transition';

const still = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

export function unfold(node: Element, { duration = 300 }: { duration?: number } = {}): TransitionConfig {
  if (still()) return { duration: 120, css: (t) => `opacity: ${t}` };
  const style = getComputedStyle(node);
  const height = parseFloat(style.height);
  const [top, bottom] = [parseFloat(style.paddingTop), parseFloat(style.paddingBottom)];
  const [marginTop, marginBottom] = [parseFloat(style.marginTop), parseFloat(style.marginBottom)];
  return {
    duration,
    easing: cubicOut,
    css: (t) => `overflow: hidden; opacity: ${Math.min(1, t * 1.6)}; height: ${t * height}px;`
      + `padding-top: ${t * top}px; padding-bottom: ${t * bottom}px;`
      + `margin-top: ${t * marginTop}px; margin-bottom: ${t * marginBottom}px;`
  };
}
