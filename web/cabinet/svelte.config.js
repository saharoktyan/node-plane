import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** The cabinet is a single-page app: the relay serves index.html for every
 * path that is not a file, and the app routes in the browser. */
export default {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({ fallback: 'index.html', strict: false }),
    // The page runs only its own scripts: the build pins them by hash. The
    // relay adds who may frame it, which a page cannot say for itself.
    csp: {
      mode: 'hash',
      directives: {
        'default-src': ['self'],
        'script-src': ['self'],
        'style-src': ['self', 'unsafe-inline'],
        'img-src': ['self', 'data:', 'blob:'],
        'font-src': ['self', 'data:'],
        'connect-src': ['self'],
        'object-src': ['none'],
        'base-uri': ['self'],
        'form-action': ['self']
      }
    },
    alias: { $locale: 'locale' }
  }
};
