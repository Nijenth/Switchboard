# ADR 0029 — Recorded demo replay

Expose a case timeline through `v_case_timeline` using the existing internal
reporting role. The view returns timestamped events, a fixed set of summary
columns, and only recognized lookup outcomes. It links audit events to cases
through tenant-scoped entity references. Action and draft rows show their latest
recorded state. Audit rows preserve the history of recorded transitions. The
reporting role still lacks access to raw tables.

Both views use the owner's permissions and therefore bypass tenant RLS as the
table owner. Migration 028 installs only the metadata timeline. The disposable
core integration harness installs `demo_case_timeline` with redacted message
text and draft bodies for the fictional demo. These columns stay out of the
production view because redaction is incomplete, as shown by the held-out
probes in [ADR 0028](0028-rules-based-pii-redaction.md).

The disposable demo harness resolves its fixture cases by order and sender.
It passes their runtime IDs and verified checkpoint sequence to
`scripts/demo_report`. The exporter verifies the disposable database marker,
queries only the demo view as `switchboard_reporting`, checks the recorded
evidence for each badge, and emits an offline HTML replay. It uses the
PostgreSQL client already available inside the test
container and Python's standard library. The exporter masks emails in displayed
text and HTML-escapes all database values. Sender fields, raw messages,
connector payloads, and audit details are excluded from the view.

The final audit scene uses the refund case's timeline alongside the verified
checkpoint from the same run. Keep this export confined to the fictional
demo fixtures. The production reporting view is an internal cross-tenant
projection.
