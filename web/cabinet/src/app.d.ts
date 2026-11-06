// See https://svelte.dev/docs/kit/types#app.d.ts
declare global {
  /** Set by Vite: the app runs on mocks (`npm run dev:mock`). */
  const __MOCK__: boolean;
  namespace App {}
}

export {};
