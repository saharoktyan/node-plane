import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** The cabinet is a single-page app: the relay serves index.html for every
 * path that is not a file, and the app routes in the browser. */
export default {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({ fallback: 'index.html', strict: false }),
    alias: { $locale: 'locale' }
  }
};
