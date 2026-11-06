# Web cabinet

The web cabinet and administration of Node Plane: SvelteKit (a single-page
app, static build) and TypeScript. The relay serves it with the API on the
same address; the same app becomes the Telegram Mini App.

- `/` — the member's cabinet: my services, the Inbox, settings (language,
  theme, password, passkeys, sessions).
- `/admin` — the administration, for staff: overview, people, nodes,
  services, announcements, system. Its pages load only when opened.
- `/login`, `/register`, `/i/<token>` — sign-in, open registration, invites.

## Development

```sh
npm ci
npm run dev:mock      # on mocks, no controller needed
npm run dev           # against an API on NODE_PLANE_API (default http://127.0.0.1:8080)
```

Mocks follow the address: `?scenario=default|empty|many|errors`,
`?role=owner|admin|operator|support|member`, `?lang=ru|en`; the choice stays
for the tab, `?scenario=reset` drops it. Any login signs in; the password
`wrong` is refused.

## Structure

- `src/lib/api/schema.d.ts` — types from the contract (`api/openapi.json`),
  `npm run gen:api` after the API changes. Mock data (`src/mocks/fixtures.ts`)
  is typed by it, so a contract change the cabinet does not follow fails
  `npm run check`.
- `src/lib/services/registry.ts` — each service type's screens. VPN has its
  own module; services from manifests use the generic one, which draws what
  the type declares.
- `locale/<language>/<section>.json` — texts, one file per section; `npm test`
  checks that every language has the same keys.

## Checks

```sh
npm run check   # types and Svelte
npm test        # unit tests, catalogs
npm run build
node scripts/smoke.mjs   # every page on mocks in Chromium (NODE_PLANE_CHROMIUM)
```
