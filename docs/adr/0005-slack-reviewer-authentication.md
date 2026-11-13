# ADR 0005 — Authenticate Slack approval interactions in core

**Status:** Accepted

**Date:** 2026-11-13

Core verifies the Slack v0 HMAC over the original form body and timestamp before parsing the interaction. The Slack app ID and signing secret come from injected configuration. Slack identifies the actor and workspace. The database maps those IDs to an active staff member in the request's tenant and checks the original Slack channel and message timestamp.

The core callback accepts approve, reject, and escalate buttons. The button value contains only the approval request UUID. n8n can forward the original signed bytes or direct Slack to the callback. Database and HTTP retry handling record a single decision.

An unconfigured Slack app cannot accept callbacks. The callback does not send Slack messages or use response URLs. Slack notification delivery and the UI for editing a draft are separate workflow concerns.
