# ADR 0006 — Issue single-use grants for mock execution

**Status:** Accepted

**Date:** 2026-11-13

Core issues a random grant and stores its digest with the action attempt. The worker authenticates independently, then consumes the grant before the mock call. The database releases the approved input only on successful consumption and checks the action, worker, lease, payload, destination and current case policy revision. Completion uses the consumed grant and the same fixed lease. Leases cannot be renewed. Workers must bound connector timeouts to the remaining lease.

An expired attempt goes to `unknown_outcome`. A separately authenticated reconciler records evidence obtained by looking up the attempt in the mock system. An inconclusive lookup leaves the action unresolved. Conclusive evidence can record an execution or a confirmed absence of effects. A confirmed absence is terminal in this implementation. Automatic retry requires an adapter contract that can establish safe replay with the original idempotency key.

Stage 2 supports the mock adapter. Grant enforcement at the real provider credential boundary remains a Stage 3 requirement.
