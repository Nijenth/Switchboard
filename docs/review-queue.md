# Review queue

Work built ahead by Claude (Fable) on stacked branches. Nothing here is merged
or pushed. Review and merge the slices **in order**; each branch is created
from the one before it.

Evidence labels: **observed** = I ran it and saw the result on this machine.
**inferred** = follows from the code, not run. **unverified** = not checked.
A local pass is not a hosted CI pass, and the builder's report is not an
independent review.

How to run the standard checks (Bash, Python 3.12 virtualenv active, Docker
Desktop running, pinned images cached as in `docs/demo.md`):

```bash
python scripts/scan_secrets.py
(cd services/core && python -m ruff check . && python -m ruff format --check . && python -m mypy app harness)
python scripts/load_packs --check
(source scripts/test_db_env.sh; docker compose --env-file .env.example --profile test run --pull never --rm --no-deps db-check)
bash scripts/test_core.sh
bash scripts/test_core.sh --offline-ai
bash scripts/test_core.sh --offline-n8n
```

## Fix pass after Codex's review (2026-11-15)

Pre-fix tips, recorded before any fix. Each is also a local tag, so the
reviewer can diff only the fixes even though branches 02 and 03 are rebased.
The tags are local only; nothing is pushed.

| Branch | Pre-fix tip | Local tag |
|---|---|---|
| `build/01-live-stack` | `0e54dab0cdc8b7ad8e4f8d41fb77db4c07122efe` | `pre-fix/01-live-stack` |
| `build/02-live-console` | `23e8993379781e2d82c1e1c99718a667c6472bcb` | `pre-fix/02-live-console` |
| `build/03-gate3-grants` | `6659f289359035f86c46ac862b9b9072e925cdde` | `pre-fix/03-gate3-grants` |

Fixes are new commits. Branches 02 and 03 are rebased onto the fixed branch
below them, so their original commits get new hashes with the same content.

## Note on the briefs

The slice A and B briefs were not in the repository. Slice A was built from
the summary of the live-input brief saved in the planning notes on 2026-11-15
(design note first; extract the harness from `test_n8n_outbound.py`; core
in-process via uvicorn; n8n as one-shot CLI calls; `scripts/demo_live
up|down`). If the full brief asks for more, that part is not built.

---

## Slice 1 — live demo stack (slice A)

- **Branch:** `build/01-live-stack` (from `main` at `1a22d19`)
- **Commit:** see `git log -1 build/01-live-stack`
- **Baseline before the change (observed):** `test_core.sh --offline-n8n` on
  `main`: 165 passed.

### What it does

- `docs/adr/0030-live-demo-stack.md`: the design note, including the exit for
  every new live state.
- `services/core/harness/` (new package beside `app`, never imported by it):
  - `live_stack.py`: the stack assembly that used to be the body of
    `test_n8n_outbound.py` (fresh scoped credentials, tenant fixture setup,
    local Slack fixture, core on a loopback port, checkpoint process, scratch
    n8n volume, one-shot n8n CLI runs, signed reviewer click). New:
    `send_email`, which pushes one new message through `wf_ingest_email`.
  - `demo_live.py`: `up [--smoke]`, `down`, `status`.
  - `checkpoint_child.py`: makes the checkpoint fixture exit when its parent dies.
- `scripts/core_stack.sh`: the database and gateway bring-up moved out of
  `scripts/test_core.sh` unchanged, so `test_core.sh` and `demo_live` share it.
- `scripts/demo_live`: the wrapper command.
- `tests/test_n8n_outbound.py` now builds its stack through the package. Every
  earlier assertion and both lost-completion fault injections are kept. One
  assertion block is new: a message that is not in the imported fixture
  (NW-1002) produces an automatic, sent, order-status reply.
- `tests/test_postgres.py` imports `NORTHWIND`, `set_northwind` and
  `verify_cluster` from the package instead of defining its own copies.
- CI: the Python job now runs `mypy app harness`.
- No migration, table, tool, dependency or workflow export changed. No file
  under `services/core/app` changed.

### How to test it

1. Run the standard checks above.
2. Live path:
   ```bash
   bash scripts/demo_live up --smoke     # terminal 1; wait for "Live demo stack ready"
   bash scripts/demo_live status         # terminal 2: "running | core http://127.0.0.1:…"
   bash scripts/demo_live up             # terminal 2: refused, exit 1
   bash scripts/demo_live down           # terminal 2: terminal 1 exits by itself
   docker ps -a --format '{{.Names}}' | grep core-check     # nothing
   docker volume ls -q | grep switchboard-n8n-test          # nothing
   ```
   Expected smoke output: `Smoke: NW-1002 | ORDER_STATUS: auto | draft sent`
   and a reply naming USPS.
3. Crash path: start `up`, close its terminal (or kill its `bash` and `python`
   processes), wait 20 seconds, then `status` prints `stale` and `down` removes
   the containers, networks, volume and state file.

### Verified (observed, on this Windows machine, final revision of the code)

| Check | Result |
|---|---|
| `scan_secrets.py` | 0 findings |
| `ruff check .` / `ruff format --check .` (services/core) | passed / 43 files formatted |
| `mypy app harness` | no issues, 31 files |
| `load_packs --check` | validated ecommerce_support ×3, invoice_processing, sales_leads |
| `db-check` (SQL) | exit 0; 301 PASS lines, 0 FAIL lines |
| `test_core.sh` | 147 passed, 18 skipped |
| `test_core.sh --offline-ai` | 164 passed, 1 skipped |
| `test_core.sh --offline-n8n` | 165 passed, 0 skipped |
| `demo_live up --smoke`, `status`, second `up`, `down` | behaved as in "How to test it"; no containers, volume or state left |
| Hard kill, then `down` | stale detected; containers, networks, volume and state removed; no checkpoint process left |

The first hard-kill test found one real defect: the checkpoint fixture process
survived its parent. `checkpoint_child.py` fixes it and the test was repeated.

### Not verified

- **Hosted CI:** unverified. Nothing was pushed. The `mypy app harness` CI
  change has only run locally.
- **Linux/macOS:** unverified. `demo_live` was only run under Git Bash on
  Windows. The n8n harness has always required Docker Desktop's
  `desktop-linux` context; that is unchanged.
- **Ctrl+C in the `up` terminal:** inferred from the `KeyboardInterrupt`
  handler and the shell traps. Only `down` and a hard kill were exercised.
- **Temporary folder removal after a crash:** inferred. The crash tests ran
  before that clean-up line was added; the full check suite ran after it.
- **12-hour ingress credential expiry:** inferred from the SQL. Not waited out.
- **PowerShell runner (`scripts/test_core.ps1`):** not changed and not run.
- **Independent review:** none yet. I wrote and tested this slice myself.

### Owner must do

- Paste the full slice A and B briefs if they differ from the summary above.
- Ask Codex for the independent review of this branch before merging.
- Push the branch and confirm hosted CI.

## Review fixes for slice 1

Pre-fix tip `0e54dab0cdc8b7ad8e4f8d41fb77db4c07122efe`.
Review range `pre-fix/01-live-stack..build/01-live-stack`.

Commit `89374ef` implements items 1�4, including exclusive ownership before
resource creation, checked cleanup, sanitized subprocess timeouts and the
public lifecycle regression test. The follow-up adds its unit tests to CI.

The previous builder reported SQL 301 PASS / 0 FAIL, core plain 157 passed /
19 skipped, offline-ai 174 / 2 and offline-n8n 176 / 0 at `89374ef`. These
are reported-by-builder results. This continuation independently observed
the same harness unit tests pass on the descendant Branch 02. Hosted CI
remains unverified. The CI-only follow-up changes no runtime code.
---

## Slice 2 — live demo console (slice B)

- **Branch:** `build/02-live-console` (from `build/01-live-stack`)
- **Commit:** see `git log -1 build/02-live-console`

### What it does

- `docs/adr/0031-live-demo-console.md`: design, browser-facing protections,
  exits for each new live state, and one deviation from the brief (below).
- `services/core/harness/console.py` and `console_page.py`: a browser console
  served by `scripts/demo_live up --console` at `http://127.0.0.1:8780`.
  Six scripted messages, a free-text form, a case feed with evidence, and a
  reviewer panel with Approve and Reject.
- Sends go through `wf_ingest_email` and the Slack-posting and dispatch
  workflows. Decisions go through core's existing signed `/approvals/slack`.
  The display reads the replay's reporting-login timeline reader.
- `tests/test_console.py`: 20 unit tests with a fake backend (token, foreign
  host name, input validation, one step at a time, failure handling, reviewer
  per role, badges from evidence only).
- `tests/test_n8n_outbound.py`: one more block. Through the console, free
  text waits for review, the fixture admin approves it, and the reply is sent.
- CI: `tests/test_console.py` added to the Python job's unit test list.
- No file under `services/core/app`, no migration, tool, pack, workflow export
  or dependency changed.

### Deviations from the brief summary

- **"Separate process":** the console is a separate app on a separate port
  inside the `demo_live up` process, not a separate OS process. ADR 0031 gives
  the reason. Say if you want a real process boundary.
- **Keyword stand-in classifier:** not built (the brief made it optional).

### How to test it

1. Run the standard checks at the top of this file.
2. `bash scripts/demo_live up --console`, wait for "Demo console", open
   `http://127.0.0.1:8780`.
3. Click "Customer asks for a refund (you decide)". After up to a minute the
   reviewer column shows two reviews: the reply draft (role agent) and
   `order_refund_issue · EUR 210.00` (role manager). Approve the refund: the
   card gains "Mock refund paid after approval". Reject the reply draft.
4. Click "Email that tries to take control": badge "Escalated to a person",
   nothing to review, draft marked not sent.
5. Send free text as Liam Chen ("Do you sell gift cards?"): "Waiting for
   review". Approve it: "Reply sent after review".
6. `bash scripts/demo_live down` from another terminal.

### Verified (observed, this Windows machine)

| Check | Result |
|---|---|
| `scan_secrets.py` | 0 findings |
| `ruff check .` / `ruff format --check .` | passed / 46 files formatted |
| `mypy app harness` | no issues, 33 files |
| `load_packs --check` | passed |
| `db-check` (SQL) | exit 0; 301 PASS lines, 0 FAIL lines |
| `test_core.sh` | 167 passed, 18 skipped |
| `test_core.sh --offline-ai` | 184 passed, 1 skipped |
| `test_core.sh --offline-n8n` | 185 passed, 0 skipped |
| Browser run of steps 3 to 5 above | Each outcome appeared as described. `down` then stopped the stack. |

### Not verified

- **Two labels in the page after the browser run:** the browser run used the
  build before I changed the draft caption ("Reply sent (mock outbound)" /
  "Draft, not sent (status: …)") and the wait-time wording. The data behind
  the caption (`reply_status`) is covered by the unit and n8n tests; the
  caption's rendering in the page is **inferred**.
- **The other four scripted buttons** (verified order status, unknown
  sender, expired, already refunded) were not clicked in the browser. Their
  messages are the ones the n8n test already sends; the console path for them
  is inferred.
- **Narrow-screen layout, keyboard-only use, screen readers:** unverified.
- **Port 8780 already in use:** inferred from the code, not exercised.
- **Hosted CI, Linux/macOS, independent review:** as for slice 1.
- **Observed limit worth knowing before presenting:** the stand-in draft model
  answered "Do you sell gift cards?" with the returns policy. That is the
  fixture's behaviour and the reason the reply waits for a reviewer.

### Owner must do

- Rehearse the console once before showing it to anyone.
- Ask Codex for the independent review of this branch.

---

## Slice 3 — release gate 3: connector gateway

- **Branch:** `build/03-gate3-grants` (from `build/02-live-console`)
- **Commit:** see `git log -1 build/03-gate3-grants`
- **Owner decision this slice rests on (2026-11-15):** real provider
  credentials live in a separate connector gateway, not in n8n.

### What it does

- `docs/adr/0032-connector-gateway.md`: options, the chosen protocol, what
  changes in AGENTS.md §2.1 (not applied here), what is not built, exits.
- `services/connectors/` (new service, own requirements, no database access):
  - `gateway/main.py`: `POST /execute {action_id}`. Claims and consumes the
    grant with core as `adapter: "real"`, refuses anything it must not act on,
    performs one provider write, completes.
  - `gateway/shopify.py`: `order_refund_issue` through `refundCreate`, with a
    search for an earlier refund carrying the same idempotency key.
  - `gateway/gmail.py`: `send_reply` through `users.messages.send`, with a
    search for an earlier message with the derived Message-ID.
  - `gateway/providers.py`: searches retry (3 attempts, backoff); writes are
    sent once and classified as refused or unknown outcome.
  - `tests/`: 46 tests against a stub core and synthetic fixtures.
- CI: new `connector-gateway` job. Runbook entry added.
- **Unchanged:** `services/core/app`, migrations, packs, workflow exports,
  AGENTS.md. Core still answers 409 to `adapter: "real"`, so nothing real can
  execute.

### One refinement to what I described when asking

I said n8n would pass "an action ID and grant". Built stricter: n8n passes the
action ID only and the gateway claims the grant itself, because core binds a
grant to the worker that claimed it. The grant never passes through n8n.

### How to test it

```bash
cd services/connectors
python -m pip install -r requirements-dev.txt
python -m ruff check . && python -m ruff format --check . && python -m mypy gateway
python -m pytest -q
```

Then the standard checks at the top of this file (unchanged code paths).

Things worth reading rather than running: the fail-closed checks in
`gateway/main.py` between consume and the provider call, and
`providers.write` (the rule that decides "refused" versus "unknown").

### Verified (observed, this Windows machine)

| Check | Result |
|---|---|
| Gateway `ruff check` / `ruff format --check` / `mypy gateway` | passed / passed / no issues, 5 files |
| Gateway `pytest` | 46 passed |
| `scan_secrets.py` | 0 findings |
| Core lint and `mypy app harness` | passed |
| `load_packs --check` | passed |
| `db-check` (SQL) | exit 0; 301 PASS lines, 0 FAIL lines |
| `test_core.sh` | 167 passed, 18 skipped |
| `test_core.sh --offline-ai` | 184 passed, 1 skipped |
| `test_core.sh --offline-n8n` | With the slice 2 test: 3 of 5 runs passed (see below). With the follow-up test change: 5 of 5 runs, 185 passed each. |

### Intermittent n8n timeout in the console test (slice 2's test; follow-up commit on this branch)

**What was observed.** With the console block of `tests/test_n8n_outbound.py`
driving the console through the in-process `TestClient` (as committed in
slice 2), `--offline-n8n` gave:

| Run | Result | Where |
|---|---|---|
| slice 2 final run | 185 passed | |
| slice 3 run 1 | 1 failed | console approve → dispatch, HTTP timeout at "Consume grant" |
| slice 3 run 2 | 185 passed | |
| slice 3 run 3 | 1 failed | console send → Slack posting workflow, HTTP timeout |
| slice 3 run 4 | 185 passed | |

So 2 failures in 5 runs. Both were an n8n → core HTTP timeout (the nodes
allow core 5 seconds), both inside the console block, at different steps. The
second failing run was not slow, so machine load does not explain it. In the
same runs, the hundreds of n8n calls the test makes directly never timed out.

**What I changed (one change, test only).** The console block now talks to
the console over real HTTP on a loopback port (`serve` + `httpx`), which is
how the browser uses it. No timeout was raised and no retry was added.

**Result.** 5 runs, 5 × 185 passed. Together with the browser session in
slice 2 (seven real-HTTP console steps, no failure), the evidence points at
the in-process test client as the trigger.

**What this does not prove.**

- **Cause: not established.** I do not know why driving the console through
  `TestClient` makes core slow to answer n8n. This is an inference from 5
  clean runs after 2 failures in 5; with a true failure rate of 40%, 5 clean
  runs would still happen about 8% of the time by chance.
- If the timeout ever appears with the real-HTTP test or in a live console
  session, it is a real defect in the console path. Stop and investigate; do
  not raise the 5-second node timeout to hide it. Per the "second fix in the
  same failure class" rule, the next step would be a design note, not
  another change.

**Related gap found by reading (inferred, not observed):** an n8n CLI run that
exceeds its 100-second limit raises `subprocess.TimeoutExpired`, which
`LiveStack.n8n` does not convert to `StackError`. In the console that would
be an unhandled 500 instead of the 502 message. Not fixed here.

### Not verified — read this before trusting the adapters

- **No request has ever been sent to Shopify or Google.** The request and
  response shapes come from my knowledge of the public API documentation.
  The fixtures are synthetic, not recorded. Specific points I could not
  confirm: the exact `RefundInput` and `Order.refunds`/`transactions` field
  names for the API version you will use; that the order search by
  `name:"…"` returns the order with or without the `#` prefix; that Gmail
  preserves a caller-supplied `Message-ID` on send and that `rfc822msgid:`
  finds sent mail.
- **Gateway against the real core:** unverified. The stub mirrors the
  `Consume` and `Complete` request models in `app/api/execution.py` as I read
  them. There is no test that runs the gateway against core.
- **End-to-end real dispatch:** not possible yet (by design, see ADR 0032).
- **Hosted CI for the new job, independent review:** none.

### Owner must do

1. Have Codex review ADR 0032 and the gateway, especially the refused/unknown
   classification and the pre-provider checks.
2. Decide whether to update the AGENTS.md §2.1 table as ADR 0032 describes.
3. Create a Shopify development store and a test Gmail mailbox; record real
   responses for each fixture file in `services/connectors/tests/fixtures/`
   and replace the synthetic ones; fix any shape differences.
4. Choose and configure the Shopify API version (there is no default).
5. Commission, as its own reviewed slice: the migration that lets claim,
   consume, complete and reconcile work for `adapter = 'real'`; the
   reconciler lookup endpoint on the gateway; the n8n real branches; Gmail
   OAuth token refresh.
