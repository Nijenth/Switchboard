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

### Provider rules

| Tool | Provider call | Duplicate protection |
|---|---|---|
| `order_refund_issue` | Shopify Admin GraphQL: find the order by name, then `refundCreate` | The refund note carries `switchboard:<idempotency_key>`. The gateway looks for that note first and returns the existing refund instead of creating a second one. |
| `send_reply` | Gmail `users.messages.send` | The Message-ID is derived from the idempotency key. The gateway searches `rfc822msgid:` first and returns the existing message instead of sending again. |

The gateway never retries a write. Searches (which change nothing) retry up to
three times with exponential backoff on 429 and 5xx. Every provider call has a
timeout bounded by the remaining lease.

If the outcome of a write is unknown (timeout, 5xx, connection lost), the
gateway reports that to the caller and does **not** complete the action. The
lease expires and the action becomes `unknown_outcome`, exactly as ADR 0006
and ADR 0026 already define. The search-first rule is what makes a later
lookup safe.

## What is deliberately not built here

1. **Core still refuses real dispatch.** `/actions/{id}/consume` answers 409
   for `adapter: "real"`, and the claim, consume, complete and reconcile
   database functions accept only `adapter = 'mock'`. Enabling real dispatch
   needs a migration that generalises those security-definer functions. That
   is the most sensitive change in the project and should be written and
   reviewed on its own, not batch-built. Until then nothing real can execute,
   even with credentials configured. The gateway is tested against a stub of
   core's documented API.
2. **Reconciler lookup through the gateway.** The search functions exist in
   the adapters; the endpoint the reconciler would call does not.
3. **n8n real branches.** The exported workflows are unchanged.
4. **Gmail OAuth token refresh.** The gateway takes an access token from
   configuration. A refresh flow is needed before real use.
5. **Read tools on real adapters** (order lookup, identity check).

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

- The provider request and response shapes were written from knowledge of the
  public API documentation. The fixtures are **synthetic**, not recorded from
  a real store or mailbox. No request has been sent to Shopify or Google.
- The Shopify API version is required configuration and is not defaulted,
  because no version has been verified.
- Whoever can read the gateway's environment can use the provider
  credentials. The gateway reduces who that is; it does not remove the need
  to protect its host.
