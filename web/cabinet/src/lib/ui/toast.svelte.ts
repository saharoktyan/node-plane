/** Short news of a finished action, over the page rather than in it, so
 * nothing on the page moves; each goes by itself after a few seconds. */
class Toasts {
  items = $state<{ id: number; text: string }[]>([]);
  #next = 0;

  show(text: string) {
    const id = this.#next++;
    this.items = [...this.items.slice(-2), { id, text }];
    setTimeout(() => (this.items = this.items.filter((item) => item.id !== id)), 3500);
  }
}

export const toasts = new Toasts();
export const toast = (text: string) => toasts.show(text);
