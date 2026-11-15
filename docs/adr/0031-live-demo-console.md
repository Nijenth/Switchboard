# ADR 0031 — Loopback demo console

**Status:** Proposed (built on `build/02-live-console`, not yet reviewed)

**Date:** 2026-11-15

## Decision

`scripts/demo_live up --console` serves a browser console for the live stack
(ADR 0030) at `http://127.0.0.1:8780`. A presenter sends a scripted or
free-text customer email, watches what the system did, and approves or rejects
the reviews it raises.

The console is `services/core/harness/console.py`: a second FastAPI app on its
own loopback port. `services/core/app` is unchanged and has no console route.

| Concern | Choice |
|---|---|
| Sending a message | `LiveStack.send_email`, then the Slack-posting and dispatch workflows. The same n8n path as the recorded demo. |
| A reviewer decision | A signed Slack interaction to core's existing `/approvals/slack`, as the seeded staff member for the required role (manager → Marco; anything else → Alex, the admin). The database still checks the role. The console cannot approve by any other route. |
| What is displayed | The replay's reader (`scripts/demo_report.read_timeline`): the reporting login, the disposable-cluster marker check, and the `demo_case_timeline` view that exists only in the disposable database. Email-like text is masked, as in the replay. |
| Outcome badges | Derived from the recorded timeline rows of that case, never from the scenario that was clicked. A pending money action is not shown as paid; an unsent draft is labelled "not sent". |
| Credentials | Stay in the harness process. The page receives one per-run console token and nothing else. |
| Concurrency | One stack step at a time. A second request gets HTTP 409, because the scratch n8n volume is not safe for concurrent CLI runs. |

### Browser-facing protections

The console can move mock money and send mock email, so a web page open in
another tab must not be able to drive it.

- It binds `127.0.0.1` only.
- Every `/api` call needs the per-run token that is embedded in the page. A
  page on another origin cannot read it.
- Requests whose `Host` is not `127.0.0.1` or `localhost` are refused, which
  blocks DNS rebinding.
- The page has a strict Content-Security-Policy, loads nothing external, and
  writes all dynamic text with `textContent`.

## Deviation from the brief summary

The brief summary says "a separate process". The console is a separate app on
a separate port inside the `demo_live up` process, not a separate operating
system process. Reason: that process already holds every fixture credential
the console needs (n8n key, signing secret, scoped tokens), so a child process
would have to be handed all of them and would isolate nothing. If a real
process boundary is wanted, `create_console` takes a small `Backend` interface
that can be put behind one.

## Not built

The optional keyword stand-in classifier. The brief allowed it behind a flag;
it would change the fake model that the evals also use, so it was left out.
Free text the stand-in does not know is classified as `other` and goes to a
reviewer, and the page says so.

## Every new live state and its exit (ADR 0026)

| State | Exit | Caller |
|---|---|---|
| Console server | Stops with the stack | `scripts/demo_live down`, Ctrl+C |
| Console token | Lives in memory for one run | Process exit |
| "One step at a time" lock | Released when the step returns or fails. Each n8n CLI run has a 100-second timeout, so a step cannot hold it indefinitely. | `Console._exclusive` |
| Port 8780 already in use | `up --console` fails with a clear message and the cleanup trap removes the stack | `serve` |
| Session case list | In memory only. A page reload keeps it; a restart clears it with the database. | Process exit |

## Limits

- Fictional tenant, fake model, mock shop, local Slack fixture. No real email,
  Slack message or payment.
- The deterministic draft model answers from the first retrieved knowledge
  chunk, so a free-text reply can be off-topic. That is why it waits for a
  reviewer.
- Only cases sent in the current console session are shown or reviewable.

## Changes after review (2026-11-15)

### Deviations from the brief: owner-accepted pending confirmation

Two things differ from the brief. Both are kept after review and are
**accepted by the owner pending confirmation**. Until the owner confirms,
treat them as open.

1. **The console runs in the `demo_live up` process**, as a separate app on a
   separate port, not as a separate operating-system process. Reasoning is
   under "Deviation from the brief summary" above.
2. **Six fixed scenarios plus a free-text form replace the guided composer.**
   The brief asked for a composer that guides the presenter through building
   a message. The console offers the six scripted messages of `docs/demo.md`
   as buttons and a plain sender-and-text form.

### A message that is only partly processed

Sending is two stages: deliver the message (ingest and the case pipeline),
then post reviews and dispatch. If any step fails after the console started a
send, the message stays in the list marked "Partly processed", with a Retry
button. The failed response already carries the case.

Retry never creates a second case:

- If the case is known, retry runs only the remaining stage.
- If the delivery itself failed before a case was seen, retry redelivers with
  the **same external ID and the same date**. Core's ingest recognises the
  delivery (its idempotency key and delivery digest match) and returns the
  original case.
- Sending the identical sender and text again while an attempt is unfinished
  is treated as a retry of that attempt.

### Rate limits

| Limit | Value | Exit |
|---|---|---|
| Sends and retries per minute | 6 | The window slides. The refusal says how many seconds to wait (`Retry-After`). |
| Messages per console session | 40 | Restart the stack (`down`, then `up --console`). The database is disposable, so nothing is lost that should be kept. |

A refused request does no work. Retries count toward the per-minute limit
and not toward the session cap.

### No inline script or style

The page, its script (`/console.js`) and its stylesheet (`/console.css`) are
three same-origin files. The policy is
`default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'`
with no `'unsafe-inline'`. The console token is in a `<meta>` tag of the page
and the static files hold no token. The script creates every dynamic node
with `createElement` and `textContent`; a test fails if an HTML sink such as
`innerHTML` appears in it.

### Added to the live-state table

| State | Exit | Caller |
|---|---|---|
| Partly processed message | Retry finishes it; otherwise it ends with the session | Presenter; `down` |
| Per-minute limit reached | Time | Clock |
| Session cap reached | Restart the stack | `down`, `up --console` |
