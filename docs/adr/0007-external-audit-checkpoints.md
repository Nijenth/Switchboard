# ADR 0007 — Verify the audit chain against an external checkpoint

**Status:** Accepted

**Date:** 2026-11-13

Expose a full-chain verifier through a restricted SQL function and a separately authenticated core API. It returns the sequence and full hash or the first broken sequence. The API accepts a previous checkpoint and verifies it within the same database snapshot.

Run the checkpoint command as a separate process with a directory writable only by the checkpoint operator. The database and core service accounts must have no write access to this directory. The command verifies the previous checkpoint before appending a new one and fails if the checkpoint history is missing. First use requires explicit initialization. A deployment can place this directory on independently protected storage and schedule the command from n8n.

The filesystem implementation protects the checkpoint from database-only access when deployed under separate identities. Protection from a compromised checkpoint host requires an independently retained copy or object-lock storage.
