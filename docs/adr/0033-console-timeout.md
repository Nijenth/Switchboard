# ADR 0033 — Investigation of `n8n -> core` HTTP timeouts on `build/02-live-console`

**Status:** Proposed (investigation and design note only; no code fix applied)

**Date:** 2026-11-16

**Branch / Commit analyzed:** `build/02-live-console` at `47097ef` (`Switchboard-fix01`)

---

## 1. Context

During `--offline-n8n` validation on `build/02-live-console` (including the real-HTTP loopback browser console step in `tests/test_n8n_outbound.py`), a prior run batch reported 1 failure in 8 runs of an `n8n -> core` 5-second HTTP timeout (`Dispatch1`: `List approved mock refunds/sends`, `createTimeoutError`).

Per project rules, before changing timeouts, adding retries, or modifying runtime code, we kept `services/core/app` untouched and instrumented the test harness externally to collect per-run evidence and test four hypotheses:

- **(a)** Core and the browser console share one Python process, and console work (`create_pool` per call in `StackBackend`, `run_path` of `scripts/demo_report`, `docker exec` timeline reads) starves core's uvicorn thread / GIL.
- **(b)** PostgreSQL connection limits or core's `psycopg_pool` (`max_size=4`) are exhausted while `n8n` calls core.
- **(c)** Docker Desktop CPU/disk I/O contention delays core or the containers.
- **(d)** The timeout also occurs without the console block running.

---

## 2. Instrumentation Method (Test-Only, Zero App Changes)

All measurements were collected via an external `pytest` plugin (`timing_probe.py`) loaded with `PYTEST_ADDOPTS="-p timing_probe"`, leaving the repository clean:

- **ASGI request tracing (`ProbeASGI`):** Wrapped apps passed to `harness.live_stack.serve` to emit `http_begin` and `http` events (normalized path, status, duration, response-header latency, in-flight count, active step label) for both `core` and `console`.
- **Event-loop lag & GIL overshoot:** `lag_monitor` measured wakeup drift of `asyncio.sleep(0.01)` on each uvicorn loop (`lag` per 1 s window, `lag_spike` whenever `> 100 ms`). A `probe-gil` thread measured wakeup overshoot of `time.sleep(0.005)`.
- **Process, DB, and host telemetry:** A 1 s sampler recorded process CPU, thread CPU deltas `> 30 ms/s`, Windows host CPU (`GetSystemTimes`), live `psycopg_pool` statistics (`pool_size`, `pool_available`, `requests_waiting`), and `pg_stat_activity` connection counts and query RTT. External PowerShell collectors logged Windows `Get-Counter` (`%_Processor_Time`, `%_Disk_Time`, `Avg._Disk_sec/Transfer`, `Current_Disk_Queue_Length`) and `docker --context desktop-linux stats`.
- **Step / CLI markers:** Decorated `LiveStack.n8n`, `interaction`, `send_email`, `StackBackend.*`, and `Console.*`, plus `subprocess.run` on `n8n` containers (capturing exit code, wall duration, and sanitized timeout/error tokens).

**Limitation of ASGI-level instrumentation:** `ProbeASGI` wraps the ASGI callable *inside* `uvicorn`. Any stall occurring *before* `uvicorn` dispatches `__call__` (inside Docker Desktop's `host.docker.internal` forwarding, host socket `accept()` / HTTP parsing inside `uvicorn`, or the `n8n` client prior to transmission) produces zero `http_begin` events on core.

---

## 3. Per-Run Evidence Summary (11 `--offline-n8n` Runs)

Eleven `--offline-n8n` runs completed cleanly under instrumentation: 10 with the console block active (`smoke-01`, `full-01..03`, `n8n-01..06`) and 1 with the console block locally disabled (`noconsole-01`, via a temporary non-committed `SWITCHBOARD_SKIP_CONSOLE_BLOCK=1` guard around `tests/test_n8n_outbound.py` lines 442–515, reverted via `git checkout`; 4 remaining runs of the `noconsole` batch were cancelled early once `noconsole-01` passed and pre-console failures were already confirmed in `n8n-02` and `n8n-05`).

| Run ID | Scope / Variant | Probe / Wall (s) | Outcome | Core Reqs | Core p50 (ms) | Core p99 (ms) | Core max (ms) | Core lag p99 / max (ms) | GIL max (ms) | DB RTT max (ms) | DB conns max / wait (`n8n` window) | n8n Runs | Timeout Failures |
|---|---|---|---|---:|---:|---:|---:|---|---:|---:|---|---:|---|
| `smoke-1115-233750-01` | `-k outbound` (console on) | 324.8 / — | PASS | 205 | 12.4 | 276.4 | 657.5 | 102.8 / 137.9 | 121.1 | 179.1 | 9 / `0` | 34 | 0 |
| `full-1115-234539-01` | full suite (console on) | 441.9 / 501 | FAIL (`test_demo_live:304`)* | 205 | 14.6 | 340.1 | 599.8 | 25.2 / 157.8 | 93.2 | 16.1 | 10 / `0` | 34 | 0 |
| `full-1115-235400-02` | full suite (console on) | 401.0 / 488 | PASS | 205 | 9.9 | 248.5 | 516.3 | 16.4 / 129.2 | 91.0 | 92.4 | 10 / `0` (`1` post-test) | 34 | 0 |
| `full-1116-000209-03` | full suite (console on) | 415.9 / 491 | PASS | 205 | 12.1 | 275.7 | 549.9 | 48.3 / 133.9 | 60.5 | 20.0 | 11 / `0` (`1` post-test) | 34 | 0 |
| `n8n-1116-002036-01` | `-k outbound` (console on) | 230.7 / 307 | PASS | 205 | 11.3 | 277.7 | 638.6 | 28.2 / 140.2 | 40.1 | 6.8 | 10 / `0` | 34 | 0 |
| `n8n-1116-002543-02` | `-k outbound` (console on) | 117.1 / 157 | **FAIL (`Email1`)** | 52 | 12.8 | 518.2 | 518.2 | 17.2 / 63.5 | 19.4 | 3.9 | 9 / `0` | 17 | **1** |
| `n8n-1116-002821-03` | `-k outbound` (console on) | 224.1 / 295 | PASS | 205 | 10.5 | 258.1 | 499.5 | 17.2 / 63.9 | 31.8 | 3.8 | 9 / `0` | 34 | 0 |
| `n8n-1116-003317-04` | `-k outbound` (console on) | 266.3 / 353 | PASS | 205 | 13.2 | 269.3 | 642.1 | 86.9 / 107.4 | 140.6 | 171.8 | 9 / `0` | 34 | 0 |
| `n8n-1116-003911-05` | `-k outbound` (console on) | 167.3 / 222 | **FAIL (`Dispatch1`)** | 141 | 11.7 | 245.3 | 506.7 | 31.7 / 73.1 | 35.3 | 15.9 | 9 / `0` | 19 | **1** |
| `n8n-1116-004253-06` | `-k outbound` (console on) | 222.2 / 294 | PASS | 205 | 9.0 | 278.8 | 489.1 | 19.3 / 56.6 | 25.5 | 6.1 | 9 / `0` | 34 | 0 |
| `noconsole-1116-004924-01` | `-k outbound` (console off) | 185.5 / — | PASS | 176 | 14.2 | 282.1 | 807.1 | 45.2 / 71.5 | 49.0 | 5.9 | 9 / `0` | 25 | 0 |

\* In `full-1115-234539-01`, `test_n8n_mock_outbound_loop` passed; the failure occurred later in `tests/test_demo_live.py::test_public_lifecycle_up_smoke_status_second_up_down` line 304 (`assert 1794766716.7863004 > 1794766716.7863004`), an unrelated heartbeat timestamp tie. Note also that every passing `test_n8n_mock_outbound_loop` run intentionally includes 2 non-zero `n8n` container exits (`switchboard-crash` and `switchboard-refund-crash` crash-recovery tests); those emit `codes=[]` rather than timeout errors.

### Console-Phase vs Pre-Console Telemetry (8 Passing Console Runs)

- **Core requests during console phase (`29` requests per run):** `p50` = **8.0–12.4 ms**, `max` = **72.8–140.0 ms** (vs `489.1–657.5 ms` in the `176` pre-console requests across the same 8 runs).
- **Core uvicorn loop lag during console phase:** max **16.7–21.7 ms** in 7 runs (`58.8 ms` in `smoke-01`), zero `lag_spike > 100 ms` during any `Console.*` / `console.*` step across all runs.
- **Console uvicorn loop lag:** max **16.8–31.9 ms** across `32–42` 1 s samples per run.
- **GIL overshoot during console phase:** **5.3–8.2 ms** in isolated `-k` runs (`n8n-01,03,04,06`; `57.6–93.2 ms` only when later full-suite tests also ran in `smoke-01` and `full-01..03`).
- **Console operation durations across the 8 passing runs:**
  - `console.__init__` (`run_path` of `scripts/demo_report`): `8.0–12.4 ms`
  - `console.find_case` (ephemeral `create_pool`): `19.7–39.2 ms`
  - `console.pending` (ephemeral `create_pool`): `22.1–58.4 ms`
  - `console.timelines` (`docker exec` running `psql`): `179.4–428.2 ms`
  - `Console.state`: `201.9–469.9 ms`
  - `console.ingest` / `console.advance` / `console.decide` (wrapping `n8n` CLI containers): `11.3–16.1 s` / `11.6–17.0 s` / `7.0–9.9 s`
- **Every `/actions/dispatch/*` call that reached ASGI across all 11 runs:** **3.9–26.7 ms** (`200 OK`).

---

## 4. Failure Deep-Dives

### 4.1 Failure 1 — `n8n-1116-002543-02` (`SwitchboardEmail1`, 15,000 ms timeout)

- **Step & location:** `tests/test_n8n_outbound.py:234`, `n8n execute --id=SwitchboardEmail1` (`24924.4 ms`, `start=1794769063.955`, `end=1794769088.879`), **before** `console_for_stack` (`console_steps=0`, `console_http=0`).
- **Error reported by n8n:** `codes=['createTimeoutError', 'timeout of 15000ms exceeded']`, `HTTP statuses: []`, `references: ['Gmail message', 'Gmail-shaped fixture', 'Normalize Gmail message', 'Run case pipeline', 'Synthetic email']`.
- **Exact timeline around failure (`t - start`):**
  - `+6.35 s` to `+9.44 s`: 29 requests reached `core` `ProbeASGI` and succeeded (`200`), ending with `POST /actions/{id}/complete` (`200`, `16.3 ms`) at `+9.44 s`.
  - **`+9.44 s` to `+24.92 s` (15.48 s window):**
    - **ASGI arrivals on core:** **0** `http_begin` events.
    - **Core uvicorn loop lag:** `12.3–16.2 ms` (Windows 10 ms sleep baseline; zero spikes).
    - **GIL overshoot:** `0.8–1.9 ms`.
    - **Core process & thread CPU:** `0–31 ms/s` (`+10.18 s` to `+24.22 s`), idle.
    - **Core `psycopg_pool` & Postgres:** `pools=[(2, 2, 0)]`, `db={'conns': 8..9, 'core_conns': 2, 'active': 1, 'max': 100}`, `rtt_ms = 1.1–2.6 ms`.
    - **Host CPU & Disk:** Probe `host_cpu_pct = 4.1%–7.6%` (`+11.18 s..+24.22 s`); Windows PerfMon `%_processor_time = 1.1%–8.2%`, `current_disk_queue_length = 0..1`, `avg._disk_sec/transfer = 0.0005–0.0179 s`.
    - **`bold_swanson` container:** CPU dropped from `42.4%–166.5%` to `0.08%–0.55%` (blocked waiting on socket); cumulative NetIO moved only from `34.3 kB / 36.2 kB` (`+10.30 s`) to `34.5 kB / 37 kB` (`+24.80 s`).

### 4.2 Failure 2 — `n8n-1116-003911-05` (`SwitchboardDispatch1`, 5,000 ms timeout)

- **Step & location:** `tests/test_n8n_outbound.py:257`, `n8n execute --id=SwitchboardDispatch1` (`switchboard-refund-crash-*`, `11896.5 ms`, `start=1794769949.850`, `end=1794769961.747`), **before** `console_for_stack` (`console_steps=0`, `console_http=0`).
- **Error reported by n8n:** `codes=['ECONNABORTED', 'createTimeoutError', 'timeout of 5000ms exceeded']`, `HTTP statuses: []`, `references: ['Claim action', 'Every ten seconds', 'List approved mock refunds', 'List approved mock sends', 'Load service URLs', 'Receive action ID', 'Run mock send']`.
- **Exact timeline around failure (`t - start`):**
  - Preceding `interaction approve/reject` (`POST /approvals/slack`) all returned `200` in `4.9–11.5 ms`.
  - `wf_mock_outbound.json` branches from `Load service URLs` concurrently to `List approved mock sends` (`GET /actions/dispatch/send_reply`, `timeout: 5000`) and `List approved mock refunds` (`GET /actions/dispatch/order_refund_issue`, `timeout: 5000`).
  - `+6.29 s`: `GET /actions/dispatch/send_reply` entered `ProbeASGI` (`inflight: 1`) and returned `200` in **`7.0 ms`** (`+6.30 s`).
  - **`+6.30 s` to `+11.90 s` (5.60 s window):**
    - **ASGI arrivals on core:** **0** `http_begin` events (`GET /actions/dispatch/order_refund_issue` never reached `ProbeASGI.__call__`).
    - **Core uvicorn loop lag:** `6.8–16.2 ms` (zero spikes).
    - **GIL overshoot:** `0.8–3.2 ms`.
    - **Core process CPU:** `0–16 ms/s` from `+6.37 s` onward (`busy=[]`).
    - **Core `psycopg_pool` & Postgres:** `pools=[(2, 2, 0)]`, `db={'conns': 8, 'core_conns': 2, 'active': 1, 'max': 100}`, `rtt_ms = 1.3–3.0 ms`.
    - **Host CPU & Disk:** Probe `host_cpu_pct = 3.3%–13.1%` (`+6.37 s..+11.38 s`); Windows PerfMon `%_processor_time = 1.5%–14.2%`, `current_disk_queue_length = 0..1`.
    - **`switchboard-refund-crash-3f0` container:** CPU dropped from `97.5%–189.9%` to `0.10%–0.34%` (`+8.20 s..+11.71 s`). NetIO reached `1.95 kB / 1.33 kB` at `+7.20 s`, then TX stayed flat at `1.95 kB` while RX ticked `1.4 kB -> 1.48 kB -> 1.55 kB -> 1.62 kB` until exit.

---

## 5. Hypothesis Evaluation

| Hypothesis | Verdict | Evidence (Observed vs Inferred) |
|---|---|---|
| **(a) Shared process + console work (`create_pool` per call, `run_path`, timeline reads) starves core's thread (GIL)** | **`ruled out`** *(as cause of the `n8n -> core` timeouts)* | **Observed:** Both `n8n -> core` timeout failures (`n8n-02`, `n8n-05`) happened strictly *before* `console_for_stack` even ran (`console_steps=0`, `console_http=0`). Across all 8 runs where console did execute, console-phase `core` latency stayed `p50 = 8.0–12.4 ms`, `max = 72.8–140.0 ms`, `core_lag_max = 16.7–21.7 ms` (`58.8 ms` once in `smoke-01`), and `console_lag_max = 16.8–31.9 ms`. (`create_pool` per call and `run_path` per init remain real inefficiencies worth cleaning up, but did not cause the timeouts.) |
| **(b) Postgres connection limits or core's pool exhausted during `n8n` calls** | **`ruled out`** | **Observed:** `pool_waiting_max = 0` throughout every `test_n8n_mock_outbound_loop` window (`pools=[(2, 2, 0)]` during both failures; the only `waiting=1` samples in `full-02`/`full-03` happened 3–16 s *after* `test_n8n_mock_outbound_loop` finished). Peak Postgres connections never exceeded `11` of `max_connections=100` (`8–9` during failures), `active=1` (probe query), DB RTT `0.9–3.0 ms`, and all `/actions/dispatch/*` requests reaching ASGI finished in `3.9–26.7 ms`. |
| **(c) Docker Desktop CPU / disk I/O contention** | **`ruled out`** *(for CPU / disk / DB saturation)*; **`unknown`** *(exact pre-ASGI transport stall layer)* | **Observed:** Host CPU during both stalls was `3.3%–13.1%` / `1.1%–17.0%`, disk queue `0–1`, core loop lag `6.8–16.5 ms`, GIL overshoot `0.8–3.2 ms`, and container CPU fell to `0.08%–0.55%` waiting on socket I/O with zero `http_begin` arriving at `ProbeASGI.__call__`. **Inferred / Unknown below ASGI:** Because `ProbeASGI` only instruments `app(scope, receive, send)` inside `uvicorn`, the exact layer dropping or stalling the TCP/HTTP stream before ASGI dispatch (Docker Desktop Windows `host.docker.internal` forwarding vs host `uvicorn` socket accept / HTTP parsing vs Node/Axios socket behavior in `n8n`) is **unknown** without packet/accept-hook capture. |
| **(d) It also happens without the console block** | **`confirmed`** | **Observed:** Both observed timeout failures (`n8n-02` at line 234, `n8n-05` at line 257) occurred on the pre-console code path before line 443 (`console_for_stack`). With the console block locally disabled via a temporary non-committed guard (`noconsole-1116-004924-01`), the identical pre-console path ran 176 core requests and 25 n8n runs (`core_lag_max = 71.5 ms`, `pool_waiting = 0`). |

---

## 6. Root Cause Summary

1. **Observed fact:** The `n8n -> core` timeouts (`2/10` console-enabled runs: `SwitchboardEmail1` 15,000 ms and `SwitchboardDispatch1` 5,000 ms) are **pre-ASGI stalls** occurring before `console_for_stack` is ever created. Core's uvicorn event loop, Python GIL, and PostgreSQL pool are idle when the client times out (`0` requests arrive at `ProbeASGI.__call__` for 5.60 s / 15.48 s while `n8n` sits at `<0.6%` container CPU waiting for a socket response).
2. **Inferred mechanism (exact sub-layer unverified):** Traffic between ephemeral one-shot `n8n` Linux containers (`docker run --rm`) and `core` on the Windows host traverses `http://host.docker.internal:<port>` -> Docker Desktop forwarding -> `127.0.0.1:<port>` (`live_stack.serve()` binding a `socket.socket()` passed as `sockets=[sock]` to `uvicorn.Server`). In `SwitchboardDispatch1` (`n8n-05`), `wf_mock_outbound` issues two concurrent `host.docker.internal` HTTP GET requests (`send_reply` and `order_refund_issue`); `send_reply` completed in `7.0 ms` while `order_refund_issue` never entered ASGI within 5,000 ms. Separating Docker Desktop forwarding vs Windows loopback `uvicorn` `accept()` / HTTP parsing requires a socket-accept hook and packet capture.
3. **Unrelated findings observed during the run:**
   - `StackBackend` opens and closes a `psycopg_pool` inside every `find_case` and `pending` call, executes `run_path("scripts/demo_report")` on every init, and exposes `GET /api/state` -> `timelines` (`docker exec psql`, `179.4–428.2 ms`) without holding `Console._busy`. None of these caused the observed timeouts, but they violate shared-pool hygiene and add avoidable overhead under concurrent UI usage.
   - `tests/test_demo_live.py:304` flaked once (`1/3` full runs) when two `heartbeat` samples were equal (`1794766716.7863004`).

---

## 7. Is `scripts/demo_live up --console` Affected?

- **Exposure to `n8n -> core` timeouts: Yes (same architecture).** `demo_live up --console` hosts `core` on the Windows host via `serve()` and runs `n8n` in Docker calling `http://host.docker.internal:<port>` with the same imported workflows (`wf_mock_outbound` 5,000 ms node timeouts, `SwitchboardEmail1` 15,000 ms node timeouts). Any intermittent `host.docker.internal` / Windows-host pre-ASGI socket stall applies equally to `demo_live`.
- **Exposure to in-process console starvation:** Under single-operator browser clicks (`console_page.py` has no background auto-poll timer; it calls `GET /api/state` once on load and on explicit clicks), observed GIL (`<= 8.2 ms` during isolated `-k` console phase) and core loop lag (`<= 31.9 ms`) were negligible. However, rapid refreshes or multiple tabs trigger unserialized `GET /api/state` calls (each allocating a `psycopg_pool` and spawning `docker exec psql` for `180–430 ms`) in the same Python process as `core`.

---

## 8. Proposed Fixes (Decision Needed Before Editing)

1. **Pinpoint and eliminate the pre-ASGI `n8n -> core` transport stall (primary bug):**
   - **Step 1 (small diagnostic):** Log TCP `accept()` timestamps in `serve()` (checking socket backlog/options on Windows) paired with container-side socket diagnostics / packet capture inside a failing reproduction to distinguish **Docker Desktop `host.docker.internal` forwarding** vs **Windows `uvicorn` `sockets=[sock]` / event-loop accept/HTTP parsing** (and check HTTP/1.1 keep-alive connection reuse in `n8n`).
   - **Step 2 (structural fix options based on Step 1):**
     - If Docker Desktop `host.docker.internal` forwarding is dropping/stalling connections from ephemeral containers: stop routing test/demo `n8n -> core` calls over `host.docker.internal` (for example, bind `core` in a lightweight container or sidecar on the same Docker bridge network, or reuse a persistent `n8n` container instead of `--rm` containers).
     - If Windows `uvicorn` `serve()` (`sockets=[sock]`) stalls accept/HTTP parsing: revisit `serve()` socket binding (`uvicorn` binding `host`/`port` directly when `port != 0`, Windows event loop policy, and listen backlog).
   - **Do not** mask the transport stall by inflating the 5 s / 15 s HTTP timeouts or adding retries first.
2. **Console and `StackBackend` cleanup (ADR 0031 / Slice B hygiene, independent of timeout):**
   - Pass/reuse one shared `psycopg_pool` in `StackBackend` (no `create_pool` inside `find_case` or `pending`).
   - Load `scripts/demo_report` once at module scope instead of calling `run_path` per `StackBackend.__init__`.
   - Protect or rate-limit/cache `Console.state` (`timelines` / `pending`), and optionally run the console server in its own OS process in `demo_live up --console` (as originally specified in ADR 0031) so interactive browser/UI operations and `docker exec` subprocesses are completely isolated from `core`.
