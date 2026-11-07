[Русская версия](settings.ru.md)

# Installation settings

Controller settings are shared by Telegram and Workstation. Workstation's own appearance and connection preferences are under its separate **Settings** section.

## Installation defaults

Open **Node Plane Settings → Basic → Installation defaults** in Workstation or installation defaults in the bot's administrator settings.

Choose protocols, Xray transports and port modes. Expand **Advanced options** for SNI, fingerprint, XHTTP path and AWG parameters. In Telegram, clickable parameter labels open their editors. Auto AWG mode displays the preset's port preference.

Edit values, review the draft and save. Reset draft discards unsaved changes. Defaults supply values for future deployments; edit an existing server in its own settings.

## Policies

Traffic collection is controlled by an administrator. Enable it to collect supported profile traffic statistics. Server cards also expose monthly aggregate traffic when available. Disabling profile collection affects the user-facing statistics; counters are operational observations, not a quota enforcement system.

**Requests settings** controls access requests and automatic approval rules. Check expiry and access grants when configuring approval; approving an account does not explain every protocol grant by itself. It also holds the bot's messages to visitors: the **welcome message** while requests are on, the **stub message** while they are off.

## Texts in several languages

Tariff names and descriptions, announcements and the visitor messages are written per language, in every language this version supports (now English and Russian). Only the first text you write, the source, is required. A language without its own text shows the source's, or another written translation you choose with **Show another**, so people with that language still get the message. **Make source** moves the source to another written language. The bot and Workstation share the same editor; `GET /api/v1/system/languages` lists the languages, and the API takes such a text as `{"source": "en", "texts": {...}, "fallback": {"ru": "en"}}`.

## Bot name and SSH key

Set the displayed bot name in installation settings → Telegram; like the bot's other settings, it is hidden while the bot is off. The controller public SSH key is needed only for the bot's **Install over SSH**: add it to the server's `authorized_keys` yourself. Copy it or use the bot's separate public-key message action. Keep private keys private.

## Services

The bot's **Admin → Services** and Workstation's **Settings → Services** list the installation's services by kind: system ones (the relay, the web cabinet and the Telegram bot), built-in ones that people get access to (VPN) and, later, your own services from manifests. Each shows its state and how many instances run on nodes. The relay's, the web cabinet's, Telegram's and the bot's location pages open from here, and Back returns here. Clients read the same list from `GET /api/v1/services`; an account's own services are `GET /api/v1/me/services`.

## Relay

The relay is the installation's public address: a tunnel to the controller's API that the web cabinet and other clients reach it through. It runs as a service on a node with an installed agent; the controller connects out to it and is never exposed. Set it up from the bot's **System settings → Relay** or Workstation's **Settings → Basic → Relay**: choose the node, the cabinet's domain and how the relay gets TLS (Let's Encrypt on port 443, certificate files on the node, or a reverse proxy in front of it). The same screen opens from a node: its settings add the relay to that node, and a node with the relay has **Relay settings** on its card. Back returns the way you came. One relay per installation; owners and administrators manage it. While it serves the web cabinet or the Telegram bot runs on a node, it cannot be removed: stop serving the cabinet or bring the bot back first.

The built-in Let's Encrypt mode uses TLS-ALPN-01: the domain must reach the node directly on port 443 for both certificate issuance and renewal. In Cloudflare, keep the record **DNS only** (grey cloud); its HTTPS proxy prevents this validation. To keep Cloudflare proxying, use certificate files with separately managed issuance and renewal (for example DNS-01), or a TLS reverse proxy in front of the relay. Enable gRPC in Cloudflare and use Full (strict) with a valid origin certificate. A failed validation may trigger a Let's Encrypt rate limit: check the relay journal for the retry time rather than repeatedly recreating the service.

## Web cabinet

The web cabinet is the app people open in a browser or as a Telegram Mini App. It ships with the controller's release and updates with it. Set where it is served from in the bot's **Admin → Services → Web cabinet** or **System settings → Web cabinet**, or Workstation's **Settings → Basic → Web cabinet** (also under **Services**):

- **On the relay's node**: the relay serves it beside the API on its own domain. Set up the relay first; then it is one step. The controller sends the files to the node's agent, which keeps each version whole in `/var/lib/node-plane-cabinet/`.
- **From the controller**: the controller's own server (the `node-plane-cabinet` unit) serves it without a relay and passes its API requests through under the relay's rules. Give the address and port it listens on (not 8080, the API's) and the address people open it at, for example behind your own reverse proxy. The controller then listens on the network: open only that port.

The page shows the cabinet's address, where it is served from, its state and version. To move it from one place to the other, **Stop serving** it first. The cabinet's address is the passkeys' domain and where invite links lead. Managing the cabinet is not available through the relay, not even from the cabinet itself.

## Announcements

Choose recipients, give the message a title if you want one (one line, up to 120 characters; **No title** skips it), write the message in one or more languages and preview it before sending. The title is written per language like the text; it heads the message in Telegram and names it in the Inbox. The API takes it as `titles`. Each recipient gets the text in their language, or the one chosen for it; the preview shows how many read each language and what. It also shows how many recipients get it in Telegram and how many only in the Inbox (people without Telegram, or every recipient while the bot is off). Inspect delivery results. Recipients can close a received message and reopen it in Notifications; dismiss removes it from their list.

For backups, alerts, audits and cleanup, see [Maintenance](maintenance.md).
