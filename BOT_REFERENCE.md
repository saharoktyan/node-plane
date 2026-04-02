# Node Plane Bot Reference

This document summarizes the bot screens, the main admin sections, and the small set of slash commands that are useful in regular operations.

Some buttons appear only in specific cases:
- `Admin` is shown only to admins
- `Request Access` is shown only to users without access
- `Update` in `Updates` is shown only when an update is available or already running
- search buttons usually appear only when there is more than one page

## Main Menu

### Regular User
- `Get Key`
  Choose a server and connection method, then get a link, config, or QR code.
- `Profile`
  View the profile card and access summary.
- `Settings`
  Change language, announcement sound behavior, and telemetry preferences.

### User Without Access
- `Request Access`
  Send an access request to admins.

### Admin
- `Admin`
  Open the admin menu.

## User Sections

### Get Key
- choose a server
- choose a connection method
- for `Xray`: choose transport (`xhttp` or `tcp`)
- view the connection link
- show QR code
- for `AWG`: download the `.conf` file

### Profile
- profile overview
- current methods and access summary
- `Overview`
  Traffic and usage overview

### Settings
- `Language`
- announcement sound toggle
- telemetry toggle, when telemetry is available for the profile

## Admin Menu

The admin menu is grouped by job:

- `Status`
  Bot health, pending requests, and problematic servers.
- `Requests`
  Pending access requests, request cards, approve/reject actions, and search.
- `Servers`
  Registered servers and server management screens.
- `Profiles`
  Create, edit, and delete access profiles.
- `Updates`
  Update status, manual check, auto-check toggle, and update run.
- `Settings`
  Bot title, requests/access settings, global telemetry, and full reset.
- `Announcement`
  Compose and send a broadcast message.
- `SSH Key`
  Show the bot SSH public key and setup instructions for nodes.

## Admin -> Status

Main purpose:
- see whether the bot is healthy
- see whether there are pending access requests
- see whether any servers need attention

From this screen you can jump to:
- `Requests`
- problematic servers

## Admin -> Requests

Shows all pending access requests.

Available actions:
- open a request card
- `Approve`
- `Reject`
- switch pages
- `Search Requests`, when there is more than one page

A request card usually shows:
- requester details
- username / Telegram ID / display name
- request state
- request timestamp

## Admin -> Servers

This is the main area for server operations.

### Server List
- list of registered servers
- search, when there is more than one page
- create a new server

### Server Card
- `Probe`
  Check basic connectivity and service state.
- `Bootstrap`
  Install or reinstall the node setup.
- `Advanced`
  Open extended editing and maintenance actions.

### Bootstrap

Depending on the server state, this menu can show:
- `Install Docker`
- `Bootstrap`
- `Reinstall`
- `Delete Runtime`

For bootstrap, reinstall, and delete actions, the bot can also offer:
- preserve config
- clean variant

### Advanced

Sections:
- `General`
  Title, flag, region, transport, target, public host, protocols, notes.
- `Xray`
  Host, SNI, fingerprint, TCP/XHTTP ports.
- `AWG`
  Public host, iface, port, preset, entropy.
- `Maintenance`
  Technical maintenance actions.

### Advanced -> Maintenance

Main actions:
- `Metrics`
  Host summary: kernel, uptime, loadavg, cpu usage, memory, disk, docker, and service status.
- `Check Ports`
  Check the required ports.
- `Open Ports`
  Try to open the required ports on the node.
- `Reconcile`
  Re-check and repair provisioning state.
- `Sync env`
  Rewrite `node.env` on the node.
- `Sync Xray`
  Re-sync Xray settings.
- `Full Cleanup`
  Remove deployed state and, for SSH nodes, optionally remove SSH setup too.

## Admin -> Profiles

The main profile editor for access management.

Typical actions:
- create a profile
- choose protocols
- choose protocol-specific servers
- edit an existing profile
- delete a profile
- freeze or unfreeze a profile

This is the main UI for provisioning and maintaining user access.

## Admin -> Updates

The updates screen shows:
- whether auto-check is enabled
- when the last check happened
- the last check status
- install mode
- current version
- local source path
- the last update run status
- upstream and latest version, when available
- the last error, when present

Buttons:
- `Check`
  Run a manual update check.
- `Auto-check`
  Enable or disable automatic checks.
- `Update`
  Appears only when an update is available or already running.

## Admin -> Settings

### Main Screen
- `Title`
  Change the bot title used in menus.
- `Requests`
  Open the requests and access settings submenu.
- `Global telemetry`
  Toggle global telemetry behavior.
- `Full Reset`
  Reset local state and, if chosen, remote node state as well.

### Requests & Access
- `Auth Text`
  Edit the message shown before a user gets access.
- `Request notifications: on/off`
  Toggle admin notifications for new access requests.
- `Access requests: on/off`
  Fully enable or disable the access request flow.

## Admin -> Announcement

Basic flow:
- enter the text
- review the preview
- `Edit` or `Send`
- `Cancel` if needed

## Admin -> SSH Key

Shows:
- SSH key setup status
- the public key path
- the next step

The `Details` screen is mainly useful when you need to:
- copy the public key
- understand where it should be installed
- avoid opening a shell just to inspect the key

## Useful Slash Commands

Only a few slash commands are worth keeping in regular operational use.

### `/version`

Shows the current bot version.

Useful when:
- checking what build is currently running
- verifying that a deploy actually reached production

### `/diag`

Supported forms:

```text
/diag
/diag awg <server_key>
/diag xray <server_key>
/diag traffic <profile_name> <awg|xray>
```

Practical use:
- `/diag`
  Quick general diagnostics.
- `/diag awg spb1`
  Inspect the AWG side of a node.
- `/diag xray spb1`
  Inspect Xray status, telemetry, and stats.
- `/diag traffic alice xray`
  Check how a specific profile is matched by the traffic collector.

### `/collecttraffic`

Run the traffic collector manually.

Useful when:
- testing a collector fix
- checking whether traffic samples are being written
- getting an immediate AWG/Xray summary without waiting for the scheduled job
