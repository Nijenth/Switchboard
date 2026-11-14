# ADR 0017 — Import inactive workflows into the local editor

**Status:** Accepted for local inspection

The loopback editor imports repository workflow exports on first startup and stores local
edits in a named volume. The editor is separate from disposable workflow tests and uses
local SQLite for n8n metadata. It imports no credentials and publishes no workflows.
Header Auth credentials hold scoped service tokens. Node environment access stays blocked.

Core creates mock reply actions and their approval requests from the current exact-draft
policy. n8n sequences the pipeline, posts approval requests, and consumes execution grants
through the existing mock send adapter. Approval notification supports other mock tools.
