# Performance measurements

Timing diagnostics are opt-in. Use them for a limited measurement session and
compare CPU usage with a separate run without diagnostics or a sampling profiler.
Logs contain stages, durations and correlation identifiers. Request bodies,
credentials, message text and callback arguments are omitted.

## Controller and Telegram

Add `NODE_PLANE_TIMINGS=1` to `/opt/node-plane/shared/.env`, then restart:

```sh
sudo systemctl restart node-plane-backend node-plane-telegram
sudo journalctl -u node-plane-backend -u node-plane-telegram -f -o cat
```

Timing entries start with `NP_TIMING`. Telegram entries share an `action_id`:

- `backend`: HTTP request through response JSON decoding, with the API's `request_id`.
- `render_prepare`: navigation, state lookup, markup and rich content preparation.
- `telegram`: one Telegram API call, including retries performed by the session.
- `action`: total update-handler duration, outcome and sums for the stages above.

Empty callback acknowledgements run concurrently with screen loading. The action
scope waits for them before completion, so aiogram's handler duration can remain
longer than the time required to submit the screen. `screen_submitted` entries and
the action summary's `screen_submitted_ms` report the time from handler entry until
Telegram accepts the latest screen message. This still excludes display latency
on the user's device. Text, alert and URL callback answers remain synchronous.

Catalog browsing uses a successful snapshot for up to 60 seconds, shared between
pages and concurrent requests. The first request after expiration fetches the
catalog. Successful explicit update checks invalidate the branch's snapshot in
the API process. Automatic checks in the separate worker become visible through
the catalog's TTL. Update admission always fetches a fresh catalog.

`GetUpdates` long polling is excluded. Background refreshes and deliveries outside
an active action use `action_id=-`. Stage sums can exceed total time when requests
run concurrently; they are not a serial decomposition. Rendering performed inside
individual handlers before `render_prepare` remains part of the action total.
Telegram timings end when Telegram acknowledges the call; they do not measure
when the phone displays the result.

API entries include the route template, status and `request_id`. `handler_ms`
measures dispatch through response headers; `middleware_ms` measures the remainder,
including admission and result auditing. These are wall-clock durations, not CPU
time. Streaming response-body transmission is outside this measurement.

Remove `NODE_PLANE_TIMINGS` and restart both services when finished.

## Workstation

Start the release binary with an absolute output path:

```sh
NODE_PLANE_TIMINGS_FILE="$HOME/node-plane-timings.jsonl" node-plane
```

The JSONL file includes timestamps, process ID and durations in milliseconds.
`workstation_action` and its nested backend stages share an `action_id`. Stages
include `ssh_connect_and_authenticate`, `authorization_helper`, `ssh_forward`, `http_handshake`,
`http_response_headers`, `http_response_body`, `response_decode` and
`backend_request`. The response-body stage includes a validated API `request_id`
when available. Nested durations overlap and must not be added to their parents.

`tui_draw` measures drawing and terminal submission. `input_to_next_frame` measures
key, paste or mouse-click processing until the next submitted frame. This frame
may show a loading state; it does not imply that backend data has arrived. It also
does not measure the terminal emulator's display latency. Idle frame timings are
included. SSH shell commands are represented by their enclosing action/helper,
without recording commands or their output.

Start Workstation without the environment variable to disable recording.

## CPU and memory

Collect a baseline, then repeat a fixed sequence of UI actions:

```sh
pidstat -u -r -d -p ALL 1 120 > node-plane-pidstat.txt
```

Keep the timing logs and note the action order and test start time. Repeat each
action enough times to compare median, p95 and maximum durations. Profile CPU-heavy
actions with `py-spy` in a separate run; low CPU with long wall-clock durations
usually points toward waiting for network, database or queued work.
