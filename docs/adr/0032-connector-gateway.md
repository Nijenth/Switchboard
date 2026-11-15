# ADR 0032 — Enforce execution grants in a connector gateway (release gate 3)

**Status:** Direction accepted by the owner on 2026-11-15. Built on
`build/03-gate3-grants`, not yet reviewed. Real dispatch stays disabled.

**Date:** 2026-11-15

## Problem

Release gate 3: before any real connector runs, the execution grant must be
enforced where the provider credentials live.

With the layout in AGENTS.md §2.1, n8n would hold the Shopify and Gmail
credentials. The grant check would then be a convention inside a workflow.
An edited, mis-wired or compromised workflow could refund an order or send
mail without a grant, and nothing technical would stop it.

## Options considered

| Option | Enforcement | Cost |
|---|---|---|
| A. Keep credentials in n8n | None. Convention plus tests. | Nothing new. |
| B. Core calls the providers | Strong, but the process that runs the AI path would hold money credentials. Breaks "core decides, it does not act". | No new service. |
| **C. Connector gateway (chosen)** | Strong. Only a process that has just consumed a grant can reach a provider write API. | One small service. Changes the §2.1 table. |

## Decision

Add `services/connectors`, a small service that is the only holder of provider
write credentials. It has no database access and makes no decisions.

```
n8n  wf_tool_<key> (real branch)
  │  POST /execute {action_id}            caller token, per tenant
  ▼
gateway
  1. core  POST /actions/{id}/claim       gateway's own worker credential
  2. core  POST /actions/{id}/consume     {grant, adapter: "real"}
          → the approved payload comes from core, never from n8n
  3. refuse unless: adapter is "real", the tool has a registered connector,
     the destination account is one this gateway holds a credential for
     under that tenant, and enough of the lease remains
  4. provider: search for an earlier effect of this idempotency key;
     only if none exists, perform the write once
  5. core  POST /actions/{id}/complete    {grant, adapter: "real", external_ref}
```

Refinement of what was described when the decision was taken: n8n sends only
the action ID. The gateway claims and consumes the grant itself, because core
binds a grant to the worker that claimed it. The grant never passes through n8n.

### What changes in AGENTS.md §2.1 (for the owner to apply after review)

- n8n keeps triggers, sequencing, step retries, Slack buttons, schedules and
  the mock tool branches.
- "Calling external APIs via its connectors" narrows to inbound triggers.
  Real provider **writes** (refund, send) go through the gateway. n8n holds no
  provider write credential.
- The §7 rule "the real branch refuses grants marked `mock`" is enforced by
  the gateway in step 3.

AGENTS.md itself is not edited in this slice.

### Write outcome rule

*(Design note, third fix in the `Refused`-vs-`UnknownOutcome` class.)*

Every gateway failure after `consume` maps to either `Refused` (HTTP 422, nothing
was sent) or `UnknownOutcome` (HTTP 502, never retry; let the reconciler decide),
except a parsed Shopify refund object whose transactions all settled to
`ERROR`/`FAILURE` (`ProviderFailed`, HTTP 422 `{"status": "failed"}`) and an
exhausted lease (`LeaseElapsed`, HTTP 409 before a provider request starts):

1. **`Refused` is only for checks the gateway makes before any provider write is sent:**
   configuration, remaining lease budget (`< 1s` raises `LeaseElapsed` -> HTTP 409),
   payload and header validation, pre-write order/payment validation in Shopify, and a
   duplicate search that failed before any write or clearly found no prior effect.
2. **After a write is sent, everything that is not a clean, fully parsed success is
   `UnknownOutcome` (HTTP 502)** (aside from `ProviderFailed` above), **including HTTP
   `400` and `422` (and `401`, `403`, `404`) and every Shopify `userErrors` shape**,
   unless it matches an entry in an explicit allow-list of **recorded** sandbox
   refusals (`shopify.RECORDED_REFUSALS`).
   - The allow-list starts empty (`frozenset()`).
   - **How an entry gets added:** trigger the refusal against a real Shopify
     development store (or test mailbox), save the recorded response fixture under
     `services/connectors/tests/fixtures/`, add the exact `(tuple(field), message)`
     tuple to `RECORDED_REFUSALS`, and add a regression test in
     `services/connectors/tests/test_gateway.py` exercising that fixture.
3. **Duplicate search:** any search answer suggesting an earlier send may exist or
   that cannot be parsed as a clean "found nothing" — an entry without a valid
   string `id`, a non-object entry, `resultSizeEstimate > 0` (or non-zero) or
   `nextPageToken` with no usable `messages`, or `messages` not a list — raises
   `UnknownOutcome` (HTTP 502), never `Refused`.
4. **Pre-send strictness:** provider invariants are enforced strictly before
   sending so avoidable mismatches are refused before any write:
   - `draft.thread_id` must be lowercase hex (`[0-9a-f]+`); uppercase or non-hex
     values are `Refused` before any provider call.
   - `draft.subject` must equal `draft.thread_subject` byte-for-byte after stripping
     leading `"Re: "` prefixes only (`^(?:Re: )+`, no case folding, no whitespace
     normalisation); any other difference is `Refused` before any provider call.

### Provider rules

| Tool | Provider call | Duplicate protection |
|---|---|---|
| `order_refund_issue` | Shopify Admin GraphQL: find the order by name, then `refundCreate` | Uses `@idempotent(key: $key)` and stores `switchboard:<idempotency_key>` in the refund note. The gateway checks existing order refunds for that marker first and classifies both fresh and reused refunds by their transaction nodes (`SUCCESS` with `hasNextPage == false` -> executed; `ERROR`/`FAILURE` only -> `failed` HTTP 422; `PENDING`, empty, mixed, paginated, multiple matching refunds, or `refund` paired with `userErrors` -> `UnknownOutcome` HTTP 502). |
| `send_reply` | Gmail `users.messages.send` with `threadId`, `Subject`, `In-Reply-To`, and `References` | The Message-ID is derived from the idempotency key. The gateway searches `SENT` mail (`labelIds=SENT`, `q=rfc822msgid:...`) first and returns the existing message instead of sending again, if that message is in the approved thread. |

**`refundCreate` answers with `refund: null` and `userErrors`:**

Shopify documents `refundCreate`'s `userErrors` as `[UserError!]!` with `field`
and `message`, and the mutation selects only `userErrors { field message }`.
Synthetic shapes are never trusted to mean "nothing happened": `shopify.RECORDED_REFUSALS`
starts empty, so every `userErrors` answer is `UnknownOutcome` (HTTP 502) unless
`refund` is `null` and every entry is an object whose `(tuple(field), message)`
matches an entry in `RECORDED_REFUSALS` (with non-empty string path segments and a
string `message`). A refund record returned together with `userErrors` always stays
`UnknownOutcome`. Until real sandbox refusals are recorded and allow-listed, any
`userErrors` response expires to `unknown_outcome` and reconciles by the refund note
marker.

**Gmail thread checks:**

- Before sending, `draft.thread_id` must be lowercase hex (`[0-9a-f]+`), and the
  reply's `subject` must equal the draft's `thread_subject` (the subject of the
  message being answered) byte-for-byte after dropping leading `"Re: "` prefixes
  only (exact case `"Re: "`, repeated, with no whitespace normalisation). Any other
  difference, including `re:`, `RE:`, extra spaces, or `Fwd:`, is `Refused` with
  no provider call. Gmail's documented threading rule needs the `threadId`, the
  `References` and `In-Reply-To` headers, and a matching `Subject`.
- On the `SENT` duplicate search, any response that cannot be parsed as a clean
  empty result (`messages` not a list, `resultSizeEstimate != 0` or `nextPageToken`
  with no usable `messages`, non-object entry, entry without a non-empty string
  `id`, multiple entries, or a different/missing `threadId`) is `UnknownOutcome`
  (HTTP 502), never `Refused`.
- A send counts as executed only if the provider's message carries the
  approved `threadId` and a non-empty string `id`. This applies to both the send
  response and a message found by the `SENT` search. Any other response is
  `UnknownOutcome`: the message exists or may exist, so it is neither resent
  nor completed.

**Configuration & credential constraints:**
- At startup (`Settings.model_post_init`), every Shopify account must use an HTTPS `endpoint` whose hostname equals `destination` so credentials cannot be paired with a mismatched shop endpoint.
- Gmail credentials require both `https://www.googleapis.com/auth/gmail.send` and `https://www.googleapis.com/auth/gmail.readonly`: `gmail.send` alone cannot run the `users.messages.list` (`rfc822msgid:`) duplicate check.

**Lease budget, search rules, and write refusal policy:**
- Once `consume` returns `expires_at`, the gateway sets an absolute monotonic deadline leaving an 8-second completion reserve (`expires_at - 8s`). Before **every** provider call — including each search retry and the write — the gateway recalculates remaining budget (`min(remaining, 15s)`), fails closed with HTTP 409 if less than 1 second remains, and bounds the entire HTTP call with `asyncio.timeout` plus the client timeout.
- Searches never follow redirects (`follow_redirects=False`) and accept only HTTP `2xx` JSON-object responses. Non-retryable statuses (`3xx` such as `302` with `{}` and `4xx` except `429`), unreadable bodies, or exhausting 3 attempts (`0.5s`, `1.0s` backoff) on `429`, `5xx`, or transport errors raise `Refused` (HTTP 422) without attempting a write.
- Writes are sent **at most once** with `follow_redirects=False` and never retried. Once a write is sent, **every** non-2xx HTTP status (including `400`, `401`, `403`, `404`, and `422`, as well as `3xx`, `408`, `409`, `429`, and `5xx`), transport error, timeout, or unreadable body raises `UnknownOutcome` (HTTP 502) and is **not** completed in core, so the lease expires to `unknown_outcome` and reconciles later (ADR 0006, ADR 0026).

## What is deliberately not built here

1. **Core still refuses real dispatch.** `/actions/{id}/consume` answers 409
   for `adapter: "real"`, and the claim, consume, complete and reconcile
   database functions accept only `adapter = 'mock'`. Enabling real dispatch
   needs a migration that generalises those security-definer functions. That
   is the most sensitive change in the project and should be written and
   reviewed on its own, not batch-built. Until then nothing real can execute,
   even with credentials configured. The gateway is tested against a stub of
   core's documented API.
2. **Preventing the gateway from claiming a mock action before `consume` returns 409.**
   Because `POST /actions/{id}/claim` is shared and core is unchanged in Gate 3,
   sending a `mock` action ID to the gateway currently claims an `executing`
   lease before `POST /actions/{id}/consume` (`adapter: "real"`) rejects with
   409; the unclaimed-again path relies on lease expiry + reconciliation. When
   enabling real dispatch, the **owner must choose** one of these claim
   designs:
   - **Option 1 — Adapter parameter on `/actions/{id}/claim`:** Have the
     gateway pass `adapter: "real"` (and n8n mock branches pass `"mock"`) so
     core checks the configured `tenant_packs.config.adapters` before granting
     a lease and returns 409 without mutating state on mismatch.
   - **Option 2 — Separate worker principals/routes per adapter:** Issue distinct
     worker tokens (or claim endpoints) for mock runners vs. the connector
     gateway so core derives the allowed adapter from the caller credential.
   - **Option 3 — Snapshot resolved `adapter` onto `actions` at `/decide`:**
     Persist the resolved adapter mode on the action row when proposed so
     `claim_action` enforces the adapter directly at `UPDATE ... RETURNING`.
3. **Reconciler lookup through the gateway.** The search functions exist in
   the adapters; the endpoint the reconciler would call does not.
4. **n8n real branches.** The exported workflows are unchanged.
5. **Gmail OAuth token refresh.** The gateway takes an access token from
   configuration. A refresh flow is needed before real use.
6. **Read tools on real adapters** (order lookup, identity check).
7. **The fields `send_reply` needs from core.** The Gmail adapter refuses a
   draft unless it carries a non-empty `subject`, a `thread_id`, an
   `in_reply_to` Message-ID, a `references` list ending in that
   Message-ID, and a `thread_subject` (the subject of the message being
   answered) that the reply's subject matches. Core's consume payload (latest definition:
   `db/migrations/024_order_refund_execution.sql`) carries only `id`,
   `channel`, `recipient`, `subject` and `body`, and `drafts.subject` is
   nullable. So even with real dispatch enabled, every `send_reply` would be
   refused with HTTP 422 and nothing would be sent. The migration that
   enables real dispatch must also add the provider thread ID and the parent
   message's `Message-ID`, `References` and subject to the dispatch, taken from the
   stored inbound message and never from n8n or the model, and must decide
   what subject a reply uses when the draft has none. The gateway test stub
   supplies these fields itself, so the gateway tests do not show this gap.

## Every new live state and its exit (ADR 0026)

The gateway adds no new action state. It holds a claimed action for at most
one lease (45 seconds):

| Situation | Exit | Caller |
|---|---|---|
| Claimed, consume refused | Nothing sent. The grant expires with the lease. | Core clock; a later claim |
| Consumed, gateway refuses the dispatch in step 3 | Nothing sent. Lease expires → `unknown_outcome` → reconciler finds no effect → `failed`. | `wf_action_reconciler` |
| Consumed, provider outcome unknown | Not completed. Lease expires → `unknown_outcome` → reconciler evidence. | `wf_action_reconciler` |
| Provider succeeded, completion lost | Same as today's lost completion: reconciler finds the effect by idempotency key. | `wf_action_reconciler` |

## Known limits (do not claim otherwise)

- **Real provider behavior is unverified until sandbox testing.** The provider
  request and response shapes (including `@idempotent`, Shopify refund
  transaction statuses, and Gmail `threadId`/`In-Reply-To`/`References`
  preservation and `rfc822msgid:` search under `gmail.send` + `gmail.readonly`)
  were written from public API documentation. The fixtures are **synthetic**,
  not recorded from a real Shopify development store or Gmail mailbox, and no
  request has been sent to Shopify or Google.
- **The lease deadline trusts the gateway's wall clock.** The remaining lease
  is computed as core's `expires_at` minus the gateway's own current time.
  If the gateway's clock is behind core's database clock, the gateway
  believes it has more time than it has. The 8-second completion reserve is
  the only margin. With more skew than that, a provider write can land after
  the lease expired. Core then refuses the completion and the action becomes
  `unknown_outcome`, so it is never retried blindly, but the reconciler could
  look for the effect before the late write lands and record `failed` for an
  action that then takes effect. A deployment must keep the gateway and
  database hosts time-synchronised to well under 8 seconds. No test covers
  skew. A design that removes the dependency (core returning the remaining
  seconds instead of an absolute time) belongs with the real-dispatch
  migration.
- The Shopify API version is required configuration and is not defaulted,
  because no version has been verified.
- Whoever can read the gateway's environment can use the provider
  credentials. The gateway reduces who that is; it does not remove the need
  to protect its host.
