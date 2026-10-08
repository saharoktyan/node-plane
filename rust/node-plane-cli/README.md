# Node Plane workstation assistant

## Action attribution

The controller permanently binds each workstation key fingerprint to one backend
account, independently of local installation profiles and temporary sessions.
On the first connection, choose an approved administrator explicitly (or supply
`--account` with their UUID or Telegram ID). The TUI asks for confirmation; Cancel
leaves the workstation key unregistered. Installation registers the key after verifying
the installed controller. Later sessions resolve the same account from the key,
even when more administrators exist. A conflicting account selector is rejected.

Deleting, disabling or demoting the account cannot silently rebind its key to
another administrator. Key records and audit history remain on the controller
across account deletion and backup restoration; session bearer tokens remain
short-lived. No Telegram ownership proof or additional password is implied by
this binding: registration uses the existing privileged SSH trust boundary.

The root-only `backend.workstation_cli` JSON helper also accepts `revoke-access`
and `restore-access`. Both require `version: 1`, a fresh UUID `session_id`, and
`attribution` containing `ssh_user` and `device_fingerprint`. Restore additionally
requires the original `account_id` of a currently approved administrator. These
are explicit privileged recovery actions, recorded in the audit. Revocation
blocks current tokens and future sessions; restoration never revives old tokens.
Ordinary `revoke` ends one operation's session and leaves its key binding intact.

The assistant sends the SSH username and the SHA256 fingerprint of its actual
workstation public key when obtaining a short-lived backend credential. Backend
mutations are attributed to the selected approved admin account, including a
name snapshot. View these records in Telegram Admin Settings → Diagnostics &
recovery → Workstation audit, or GET `/api/v1/system/workstation-audit` with a
maintenance-authorized credential.

Installer and offline diagnostic/recovery commands also record their operation
ID, action, step, SSH username, key fingerprint and exit status in the host journal:
`sudo journalctl -t node-plane-workstation`. Commands, input secrets and command
output are never included. Missing completion records represent unknown outcomes;
normal journal retention applies. These host records have no backend account
before installation. This covers installer/diagnostic commands and backend API
mutations. Both first-password workstation key enrollment and controller-key
enrollment on a target node have host admission/completion events, containing
only target login/address and key fingerprints. Controller-key preparation also
records the selected administrator and a shared operation UUID in backend audit;
its successful outcome requires a fresh controller-to-node key login. An SSH
disconnect or failed verification is recorded as unconfirmed, with no blind retry.
Host completion reports the key-file command exit status, not proof of SSH login.
Privileged SSH remains the trust boundary; attribution is not tamper-proof against
root and does not independently authenticate a Telegram identity.

A native Rust binary for installing, updating and checking the systemd Node Plane stack
over SSH. The workstation needs no Python interpreter or external SSH client.
The Linux VPS still uses the existing Python backend and Telegram runtime.

The first implemented slice includes a Ratatui/Crossterm full-screen interface,
concise command mode, embedded SSH and the existing installer with structured
progress. It also observes backend-owned coordinated updates and prepares SSH
access to additional nodes with the **controller's** public key.

The TUI has an action sidebar and connection fields on the right. Click an action,
field or rectangular button, or use the keyboard: Up/Down selects actions in the
sidebar, Tab moves through fields and Continue, and Enter advances or confirms.
Confirmation buttons also support Left/Right and Tab. In the right column, Esc
returns focus to the sidebar without discarding the installation editor draft.
From the sidebar, Esc asks before closing; Cancel is selected by default.
Modal dialogs ignore background clicks and host trust defaults to Cancel.
Mouse support requires a terminal that forwards mouse events; keyboard navigation
remains available. Manual Windows mouse acceptance is still pending.

Settings is anchored at the bottom of the sidebar; clicking it or selecting it
with the keyboard opens the settings directory. Hovering does not open it.
Installation profiles contains the saved connection list. Use selects a connection, Edit updates it, and New
creates one. Deleting a profile also removes its address/port from
the workstation's `known_hosts.json`, unless another saved profile uses that
endpoint. SSH keys, server state and operation history remain unchanged. An empty
saved list is authoritative and is not reimported from operation history.
Appearance offers the terminal's Cyan palette color (default), Peach `#ffbb74`,
Blue, Purple, Green, or a custom `#RRGGBB` accent. The choice is saved globally,
applies immediately, and preserves warning colors and disabled controls.
New profiles default to the `dev` channel rather than inheriting
the previous connection. Session opens read-only information about the current
TUI launch UUID, local installation UUID, administrator selector, SSH endpoint,
workstation public-key fingerprint and state directory. The launch UUID is local
to the interface; backend audit identifies each request and command within the authorized session. Viewing this
screen does not create keys or expose passwords or tokens. A quick profile switcher directly above Settings supports clickable
arrows and Left/Right when focused. It cycles through saved installations and
New profile. Clicking the selected New profile again, or pressing Enter, opens
creation. Actions are dimmed and disabled until a saved profile is selected; this also
applies on first launch and while New profile is selected. Completed connection
fields are hidden from action forms; release
tags, bot tokens and target-node inputs remain action-specific. The last selected
installation is restored on startup. Explicit controller command arguments take
precedence over remembered selection. A connection entered directly in an action
form is remembered when its confirmation is accepted. If an explicit controller
address has no matching saved profile, create a profile from the prefilled
connection details before running a TUI action. Command mode remains available
for explicit noninteractive requests.

Preferences live in `installations.json` under `--state-dir` (the default is the
user's local Node Plane application data directory). They contain names, SSH
addresses/users/ports, optional update channels and administrator identifiers.
Passwords, private keys and bot/bearer tokens are not stored in this file.
Writes are private and atomic; conflicting windows cannot overwrite newer edits.
On first use, valid existing update records seed the connection list without
modifying those operation records or replaying their operations.

Closing update observation leaves the backend worker running on the controller.
Other active SSH workflows currently close the TUI but retain the workstation
process and connection until completion; keep the terminal open as the exit
dialog instructs. Closing the interface does not request cancellation of server
work, and no new confirmation is accepted after the interface closes.

## Build and run

### Install and update the workstation itself (Linux)

Install directly without manually downloading/extracting an archive:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash
```

The download wrapper requires Bash, curl, tar and sha256sum. It checks archive
contents, SHA256 and the executable version before invoking `self install`.
The default `dev` channel includes published alpha releases. To pin a release:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash -s -- --tag v0.4.3-alpha.50
```

Use `--channel stable` instead when stable releases are published. To inspect
the installer first, download the script to a file and read it before running
`bash install_workstation.sh`. The bootstrap wrapper affects the local
workstation only and preserves saved state.

After extracting the Linux release archive, run the binary once:

```sh
./node-plane-cli-linux-amd64 self install
```

This explicitly installs `node-plane` in `~/.local/bin` and adds a marked PATH
block to `.profile` and existing Bash/Zsh startup files (creating `.zshrc` when
Zsh is the selected shell). Open a new terminal afterwards. No root privileges
are needed. Installation refuses to replace an unrelated or externally modified
binary. Run these separate commands to manage the workstation:

```sh
node-plane self update
node-plane self update --yes
node-plane self uninstall
```

Uninstall removes the managed executable and its PATH blocks; saved profiles,
SSH keys, operation records and VPS installations remain. Updates download the
published Linux x86_64 archive over HTTPS, verify its SHA256 checksum and expected
archive member, then replace the executable atomically. Restart the running TUI
to use the replacement. Concurrent local installations are refused.

Every TUI launch checks GitHub releases in the background. Stable binaries check
stable releases; alpha binaries also include prereleases. A newer usable release
opens a confirmation only when no server workflow or other dialog is active.
Declining does not update anything. Offline/rate-limited checks are shown in
Settings without interrupting server work. Settings always displays the running
version and check result; when an update exists it offers Update TUI (U).
These updates are independent of controller update channels and saved VPS profiles.
Self-installation and published self-update assets currently support Linux only;
the existing Windows source build remains available.

Rust 1.89 or newer is required to build from source. It is not required on the
machine running the resulting binary.

```sh
cargo build --locked --release --manifest-path rust/node-plane-cli/Cargo.toml
./rust/node-plane-cli/target/release/node-plane
```

On Windows the executable is `node-plane.exe`. Windows 10/11 terminal support
uses Crossterm; Windows Terminal is recommended. The CI workflow builds/tests
natively on Windows Server 2022 with MSVC and static CRT, and on Ubuntu 22.04.
This workflow has been added but its remote run and manual Windows 10/11 terminal
acceptance have not been confirmed locally. Linux terminal rendering and SSH
behavior are tested here; no production VPS installation was performed.

Examples (without `--no-tui`, an interactive terminal opens the form with these
values prefilled):

```sh
node-plane install vps.example --admins 123456789 --branch dev
node-plane diagnose vps.example
node-plane --no-tui install vps.example --admins 123456789 --tag v0.4.3-alpha.48
node-plane --no-tui diagnose vps.example
node-plane --no-tui diagnose vps.example --repair
node-plane update controller.example
node-plane --no-tui update controller.example --account 123456789
node-plane prepare-node controller.example --target vpn.example
```

The bot token is entered in a masked field. Concise mode prompts without echo;
noninteractive callers may supply `NODE_PLANE_BOT_TOKEN` in their environment.
Never put passwords or tokens in command arguments. New host trust and missing
SSH authorization require an interactive terminal; unattended runs refuse those
prompts rather than accepting hosts or guessing credentials.

## Nodes

The sidebar order is Install Node Plane, Update Node Plane, Nodes, and
Diagnostic. Nodes reads the selected controller's server registry through its
bound administrator account. Regions start expanded; click their triangle or
select the region and press Enter to fold them. Server rows open a card.

Action forms do not ask for an administrator account. An existing workstation key
binding selects the account automatically; the installation profile can retain an
explicit selector in Settings. For an unbound key with several administrators, a
separate dialog lists `Telegram ID · @username` (ID alone without a username).
Use up/down and Enter or click an account; Escape cancels registration. One
administrator skips the list. Key registration still requires confirmation, and
subsequent actions use the permanent backend binding.

Search matches server name, region and code locally, without reconnecting. Page
size follows the available terminal height, includes region headings, and repeats
the heading when a region spans pages. Page arrows and the page counter appear
only when needed. Tab/up/down select controls, Enter activates them, Ctrl+F
selects search, left/right change list pages, and Escape returns to the list/sidebar.
On a node card all four arrows follow the visible button positions; Tab and
Shift+Tab retain sequential selection. Right from the action sidebar enters the
selected section's content (the profile selector retains its switching arrows).
Escape from any Settings subpage returns to the Settings overview.
Mouse clicks and the wheel also work. Switching installation profiles clears the
browser cache; Refresh explicitly loads updated registry state.

All TUI actions share the selected installation's verified SSH connection:
installation, updates, diagnostics, recovery and Nodes. Target connections opened
by Prepare SSH are retained with that controller session too. Connections close
after five minutes without interaction, on installation profile changes, and on
exit. The next action reconnects when necessary. A fixed indicator in the
top-right corner shows SSH connected, connecting or closed without moving panels.

The backend bearer credential stays only in memory while the SSH session is open.
Actions reuse it instead of launching the privileged Python helper each time.
It is renewed after at most 25 minutes, before the backend's one-hour expiry.
An authentication/permission rejection invalidates the cache; closing the session
revokes its credential when the controller is reachable. Backend permissions and
audit still apply to each HTTP request, with separate command IDs for mutations.
Uncertain mutations are never replayed when reconnecting. Command mode closes
its connection and authorization when its single action finishes.

Cards show persisted status, selected protocols and connection type, and inspect
agent/Docker/protocol services when opened. Check status refreshes that inspection.
Agent setup appears only when the backend confirms the agent is not installed;
unchecked or unreachable agents do not offer setup. Agent setup and bootstrap
use existing backend jobs and require confirmation; Docker installation is offered
when the inspection reports it missing. Agent setup uses the stored SSH target
and port 22; custom-port agent setup remains available in Telegram. Prepare SSH
retains the existing editable target/user/port workflow. Server creation and
configuration editing remain in Telegram for this increment.

Each mutation records its command identity before dispatch. An uncertain result
blocks another mutation in the card; Operation status resolves the original command
through the administrator-scoped journal, then reads the exact backend job.
It never repeats the POST. Accepted and uncertain operations are saved privately
as `node-operation-UUID.json` without bearer tokens or passwords, so Operation status
can be reopened after a workstation restart. A confirmed terminal result may
still require refreshing the card or checking its current host status.

## Detailed diagnostics and initial recovery

Diagnostic now runs an embedded, versioned observer rather than dumping
`healthcheck.sh`. It checks managed installation files, required configuration
key presence (never their values), release metadata, disk space, PostgreSQL and
maintenance schema, controller services, worker timer/last result, API readiness
and blocked operation counts. A successful inactive oneshot worker is normal.
The summary stays above the scrollable report. Use arrows, Page Up/Down,
Home/End or the mouse wheel; Close never overlaps script output. Close, Enter and
Esc on a completed result return to the action screen with the selected
installation preserved. The next action retains the state directory and uses
a fresh operation identity; previous progress and secrets are cleared. Ctrl+C
still opens the exit confirmation. Successful results are not printed again
after leaving the TUI; exiting directly from an error result reports it in the
terminal. Command mode still prints its result.

Service recovery is disabled by default. Enable its selector in the diagnostic
form (or pass `diagnose --repair` in command mode) to offer individual confirmed
starts of stopped/failed, already installed backend, Telegram, driver or worker
timer units. Each dispatch rechecks state while holding the backend maintenance
admission guard. Active controller updates, cleanup and database restores refuse
recovery. Running services are not restarted; files, gates and operation journals
are not edited. A new full observation follows the attempts, and unresolved
errors remain errors. Database/guard failures do not permit speculative repair.

This is the first workstation recovery slice. Operation-specific cancellation,
confirmation for queued controller updates and blocked core update evidence are
available through the backend inventory when its API is reachable. Read-only
diagnostics list queued/running/blocked operation identities; recovery mode asks
before each applicable action and never replays an uncertain installation.
Inventory requires an approved administrator; the saved administrator account
field selects one when there are multiple admins. Older backends without the
inventory endpoint produce a warning while host diagnostics remain available.
The Telegram admin Settings menu now includes Diagnostics & recovery, with the
same operation inventory, paging and confirmed update/node recovery contracts.
Missing configuration/packages and other blocked agent/profile/removal/backup
work still require their specific recovery paths. This observer does not prove
end-to-end VPN traffic or Telegram message delivery. Host-level service recovery
remains workstation-only and can operate when the backend or Telegram is down.

## First SSH connection

1. Confirm the server's SHA-256 SSH fingerprint after checking a trusted source.
2. The assistant tries its local Ed25519 key. If it is not accepted, enter the
   SSH account's password. Password authentication must be enabled by sshd.
3. The assistant appends only its public key to `~/.ssh/authorized_keys`, keeping
   existing entries. It rejects unsafe symlinks/ownership and verifies a fresh
   key-authenticated connection before proceeding.
4. Subsequent connections use that key and do not ask for a password. A changed
   server fingerprint stops before authentication; trust is never silently reset.

This is a **workstation key**, distinct from the controller key used to manage VPN
nodes. Private keys are never sent to the VPS. Existing external SSH keys,
ssh-agent, keyboard-interactive MFA and encrypted key import are not supported
by this first slice. A VPS configured for key-only login needs an existing entry
for the assistant's public key before connection.

State lives in the operating system's local application-data directory:
`~/.local/share/node-plane` on Linux and `%LOCALAPPDATA%\node-plane` on Windows.
Use `--state-dir PATH` for a dedicated alternative. The directory contains
`id_ed25519`, `id_ed25519.pub`, `known_hosts.json`, operation logs and nonsecret
`update-UUID.json` records. Unix private
files are mode 0600 inside a mode 0700 directory; Windows uses a protected ACL
for the current user and LocalSystem. Password input buffers are cleared and not
persisted; the SSH library necessarily makes transient authentication copies.

## Installation and progress

Automatic prerequisites support Debian/Ubuntu with systemd, using host
Python 3.11/3.12 when available. On Ubuntu 22.04, Ubuntu 26.04,
or other supported hosts without these versions, the installer uses uv to
provision private Python 3.12 without third-party apt repositories or changes to
system Python. Host tools, uv and Docker/PostgreSQL are installed automatically.
SSH must give root access or passwordless sudo. No third-party package repository
is added. The supported controller root is `/opt/node-plane`.

The assistant downloads and verifies the controller-only release archive under
`/opt/node-plane-assistant`; it does not clone the repository. Release selection
requires published controller/checksum assets. The assistant prepares a private directory,
uploads the archive bootstrap helper plus a mode 0600 temporary configuration over
SSH stdin, and runs `scripts/install.sh --non-interactive --mode simple
--install-systemd --progress-json --env-file PATH`. It does not duplicate backend
schema, systemd, driver or PostgreSQL provisioning policy. The runtime comes from
the selected release archive, including its installer. Releases without controller
archives are not offered by the workstation installer. Shell line endings are normalized
for Linux regardless of which workstation built the binary.

The UI shows the current phase and **X/7 completed steps**, not a time estimate:
configuration, release, Python dependencies, PostgreSQL/schema, identity,
systemd services and driver/agents. Command output goes to a private local log,
with the submitted bot token and PostgreSQL URLs removed. Individual output
lines are bounded and each operation log is capped at 16 MiB. Preparation and final
verification are reported separately. Success also requires backend readiness,
active services/worker timer, stable Telegram startup and a bounded `getMe` check.
An inactive oneshot worker between timer ticks is normal.

An active installation is refused. The installer records an incomplete-install
marker and serializes attempts with `flock`; rerunning the **same release and
credentials** can resume a marked partial install while preserving generated
shared state. Explicitly pin the original tag when retrying; latest may change.
This is a deliberate retry, not automatic replay after lost SSH. Disconnects or
missing exit status produce an unconfirmed result and a log path; run diagnostics
before deciding to retry. Do not close the terminal while installation is running.
The uploaded configuration is removed when the installer command exits normally
or fails; a broken SSH transfer can leave a protected partial file on the host.

Diagnostics run the installed `healthcheck.sh --mode simple`, without printing
configuration contents or tokens. First-time SSH key enrollment remains the
same explicit connection step.

## Coordinated updates

`update HOST` discovers releases in the installed channel. Leave `--branch`
blank to keep that channel; the assistant does not silently change preferences.
Use `--tag VERSION` to select an exact allowed release, including a downgrade
with explicit confirmation. When the installed controller is current but agents
or runtimes are outdated, the existing release can be applied to the entire
stack. An unreachable agent is reported as unknown rather than confirmed current.
An already installed release is not submitted again when the driver, agents and
runtimes are confirmed current, including when an explicit tag was supplied or
the update overview still reports that release as available. Unfinished
operations retain their recovery identity and are checked before release discovery.

The assistant provisions a one-hour, scoped **account credential** through a
root-only helper on the installed controller. `--account` accepts an approved
administrator's backend UUID or numeric Telegram ID. A previously bound key
resolves its account automatically; an unregistered key requires explicit
selection even when only one administrator exists. The credential includes only
maintenance, settings and node-management scopes. The bearer token is kept in memory
on the workstation, never in command arguments or local operation records.
Root-private session state lives in `shared/data/workstation-sessions` on the
controller. Completed operations revoke the credential; unconfirmed or detached
sessions expire and can be renewed only for their original approved account.

HTTP runs through verified SSH to the controller's loopback backend. The backend
owns the update, rollback and node rollout; the assistant never invokes a second
installer over a running controller. It displays backend, worker, driver and
Telegram component states, followed by regional node pages of ten entries.
Partial node failures, confirmed rollback and blocked recovery are distinct
results. `--yes` accepts the final action confirmation for concise automation;
it does not bypass first-host trust or SSH authentication.

For an unconfirmed core update, the assistant can ask the backend to recheck
durable completion evidence for the same job. This does not repeat installation.
The installed backend must support the recovery endpoint through its maintenance
gate; older versions require a backend fix or administrator recovery. An updated
workstation binary alone cannot remove an older backend's blocked state.

Before dispatch, the assistant atomically saves its command UUID and immutable
intent. If the queue response is lost during a restart, it looks up the job by
that account and command UUID, never by the globally latest update. Uncertain
submissions are not replayed. Esc after progress appears stops TUI observation;
the backend worker continues independently. Reopening update for the same
controller resumes an unfinished record automatically, or choose one explicitly:

```sh
node-plane update controller.example --operation UUID
```

Keep the same `--state-dir`, hostname, SSH user and port when resuming. Backend
restarts are observed for up to one hour. Closing the terminal does not request
backend cancellation. Recovery of blocked backend operations remains in the
existing administration flow; the assistant does not remove safety fences.

## Prepare a target node

`prepare-node CONTROLLER --target NODE` first connects from the workstation to
both hosts, with the usual fingerprint and first-password enrollment. After
confirmation, it appends the controller's public key to the target user's
authorized keys, preserving unrelated entries. The controller then verifies a
fresh key login against the workstation-confirmed target host key before
preserving that pin in its configured known-hosts file. The controller's private
key never leaves the controller. Repeated public-key enrollment is idempotent.

Use `--target-user` and `--target-port` for a different SSH account or port. This
step verifies SSH access only; it does not create a registry node, install an
agent or protocols, or grant a profile VPN access. Complete those steps in the
bot. Target installation requires root or passwordless sudo. Controller keys
currently must be Ed25519. If final verification fails, a public key may already
have been added, but the assistant does not report successful preparation.

## Verification

```sh
cargo fmt --manifest-path rust/node-plane-cli/Cargo.toml --check
cargo clippy --locked --manifest-path rust/node-plane-cli/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path rust/node-plane-cli/Cargo.toml
```

Unix SSH integration tests bind disposable loopback servers and execute key
enrollment only with a temporary HOME. They cover first-password/fresh-key login,
password-free reuse, changed/untrusted host keys, rejected authentication,
preserved authorized keys, command input/output and missing exit status. Windows
runs portable state/key and rendering tests; the Unix mock-server tests are
excluded. They do not prove a complete install on a live VPS or real Windows
console behavior.
