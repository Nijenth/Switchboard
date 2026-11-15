# ADR 0030 — Live demo stack (design note for the live-input demo)

**Status:** Proposed (built on `build/01-live-stack`, not yet reviewed)

**Date:** 2026-11-15

## Problem

The recorded replay (ADR 0029) shows one fixed run. A live demo needs the same
mock stack to stay up and accept new messages. Today the only code that can
assemble the stack is the body of one 790-line test, so nothing else can use it.

## Decision

Move the stack assembly out of `tests/test_n8n_outbound.py` into a reusable
package, `services/core/harness/`, and add `scripts/demo_live up|down|status`.
The test keeps every assertion and every fault injection and now builds its
stack through the package, so the test stays the proof that the package works.

| Piece | Where | Notes |
|---|---|---|
| Disposable database and offline AI gateway | `scripts/core_stack.sh` | The bring-up and cleanup that used to be inline in `scripts/test_core.sh`. Both `test_core.sh` and `demo_live` source it. |
| Core | In-process uvicorn on a loopback port | The unmodified `create_app`. The harness adds the local Slack fixture route to its own app instance, as the test always did. No demo route exists in `services/core/app`. |
| n8n | One-shot `n8n` CLI containers on a scratch volume | Same pinned image and settings as before. There is no long-running n8n and no webhook listener. |
| Checkpoint process | Child process | Unchanged. |
| A new live message | `LiveStack.send_email` | Re-imports the harness's own copy of `wf_ingest_email` with that one message in the "Gmail-shaped fixture" node, then executes it. The exported workflow in `workflows/` is not changed. |

`harness` is a sibling of `app`, not part of it, so fixture code (the fake
Slack endpoint, signing a reviewer click with the fixture secret) cannot be
imported by the service. It adds no dependency: it uses packages already in
`requirements-dev.txt`.

### Why re-import instead of a webhook

Re-importing a workflow with the message baked in is the mechanism the test
already proves. A webhook needs a long-running n8n, a published trigger and a
second inbound credential. That is more new surface than a demo needs. The
cost is one extra CLI container start per message.

### What refuses to run

`demo_live up` only talks to the database container it just created. Every
login checks the per-run cluster marker before it writes (the existing
`verify_cluster` rule), so the harness cannot write to any other PostgreSQL.

## Every new live state and its exit (ADR 0026)

| State | Exit | Caller |
|---|---|---|
| Running stack (containers, volume, core thread, checkpoint child) | Stops and removes everything | `scripts/demo_live down` from another terminal, or Ctrl+C in the `up` terminal. Both run the same cleanup trap. |
| Stale state file after a crash or a closed terminal | `down` sees the heartbeat is older than 20 seconds, force-removes the recorded container, Compose project, n8n volume and temporary folder, then deletes the file. It only removes names that match the harness's own patterns. | `scripts/demo_live down` |
| Checkpoint child process | Stopped on normal exit. If the harness is killed outright, the child exits when its stdin pipe closes (`harness/checkpoint_child.py`). | The harness; the operating system |
| Plain `credentials.json` used for the n8n import | Deleted as soon as the import command returns, whether or not it succeeded | `live_stack` |
| `.demo-live/stop` request file | Deleted by `up` when it exits and by `up` when it starts | `scripts/demo_live` |
| Live ingress credential | Expires after 12 hours (`ingress_credentials.expires_at`). The database is removed with the stack. | PostgreSQL clock; `down` |
| A second `up` while one is running | Refused with a message naming `down` | `scripts/demo_live up` |

The state file (`.demo-live/state.json`, git-ignored) holds process, container,
project and volume names only. It holds no token or password. Generated
credentials live in the `up` process's memory and in the scratch n8n volume,
which is encrypted with a key that is never written to disk.

Approvals, leases and claim windows are unchanged, so their exits are the
ones in ADR 0026. One consequence for a live demo: an approved action must be
dispatched within the tenant's claim window (60 minutes by default), or the
watchdog cancels it when it next runs. That is the designed behaviour.

## Limits

- This is a demo and test harness for the fictional tenant on a disposable
  database. It is not a deployment shape.
- The fake model still decides intent. Text it has not seen maps to `other`
  (or `order_status` for "where … NW-n"), so most free text goes to review.
- Slice B (the browser console) is a separate process that drives this
  package. It is not part of this slice.
