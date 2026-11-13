-- =====================================================================
-- 002_security_hardening.sql
-- Place at: db/migrations/002_security_hardening.sql (runs after 001, before seeds)
--
-- Closes the gaps found in the architecture review:
--   A. Tenant consistency  — child rows can't point at another tenant's parent rows
--   B. Row-level security  — the app role only ever sees the current tenant's rows
--   C. Least privilege     — separate DB roles; the AI path cannot change config/permissions;
--                            nobody but the owner can UPDATE/DELETE audit records
--   D. Approval binding    — an approval is tied to the exact payload (tool, amount, input);
--                            payloads freeze once approval is requested
--   E. Action state machine — legal transitions only; 'executing' claim + 'unknown_outcome'
--                            for crash-safe external calls
--   F. Tamper-evident audit — hash chain over audit_log + verify function
--
-- Honest limits (documented in AGENTS.md §11):
--   * A PostgreSQL superuser / table owner can still bypass triggers and RLS. The hash chain
--     makes tampering DETECTABLE; anchoring the latest hash outside the DB makes it provable.
--   * Reporting views run with the owner's rights (cross-tenant). Only internal staff use them.
-- =====================================================================

BEGIN;

-- =====================================================================
-- A. TENANT CONSISTENCY (composite foreign keys)
-- =====================================================================
-- Parents expose (tenant_id, id) so children can reference both.
ALTER TABLE staff_users         ADD CONSTRAINT staff_users_tenant_id_key         UNIQUE (tenant_id, id);
ALTER TABLE queues              ADD CONSTRAINT queues_tenant_id_key              UNIQUE (tenant_id, id);
ALTER TABLE tenant_packs        ADD CONSTRAINT tenant_packs_tenant_id_key        UNIQUE (tenant_id, id);
ALTER TABLE channel_endpoints   ADD CONSTRAINT channel_endpoints_tenant_id_key   UNIQUE (tenant_id, id);
ALTER TABLE channel_endpoints   ADD CONSTRAINT channel_endpoints_source_key      UNIQUE (tenant_id, channel, id);
ALTER TABLE organizations       ADD CONSTRAINT organizations_tenant_id_key       UNIQUE (tenant_id, id);
ALTER TABLE contacts            ADD CONSTRAINT contacts_tenant_id_key            UNIQUE (tenant_id, id);
ALTER TABLE cases               ADD CONSTRAINT cases_tenant_id_key               UNIQUE (tenant_id, id);
ALTER TABLE cases               ADD CONSTRAINT cases_tenant_channel_key          UNIQUE (tenant_id, id, channel);
ALTER TABLE cases               ADD CONSTRAINT cases_source_key                  UNIQUE (tenant_id, id, channel, endpoint_id);
ALTER TABLE cases               ADD CONSTRAINT cases_tenant_id_pack_key          UNIQUE (tenant_id, id, pack_id);
ALTER TABLE messages            ADD CONSTRAINT messages_tenant_id_key            UNIQUE (tenant_id, id);
ALTER TABLE messages            ADD CONSTRAINT messages_tenant_case_id_key       UNIQUE (tenant_id, case_id, id);
ALTER TABLE documents           ADD CONSTRAINT documents_tenant_id_key           UNIQUE (tenant_id, id);
ALTER TABLE documents           ADD CONSTRAINT documents_tenant_case_id_key      UNIQUE (tenant_id, case_id, id);
ALTER TABLE ai_runs             ADD CONSTRAINT ai_runs_tenant_id_key             UNIQUE (tenant_id, id);
ALTER TABLE policy_decisions    ADD CONSTRAINT policy_decisions_tenant_id_key    UNIQUE (tenant_id, id);
ALTER TABLE policy_decisions    ADD CONSTRAINT policy_decisions_tenant_case_id_key UNIQUE (tenant_id, case_id, id);
ALTER TABLE approval_requests   ADD CONSTRAINT approval_requests_tenant_id_key   UNIQUE (tenant_id, id);
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_tenant_id_key UNIQUE (tenant_id, id);

ALTER TABLE channel_endpoints  ADD CONSTRAINT channel_endpoints_tenant_pack_fk FOREIGN KEY (tenant_id, tenant_pack_id) REFERENCES tenant_packs (tenant_id, id);
ALTER TABLE ingress_credentials ADD CONSTRAINT ingress_credentials_endpoint_fk FOREIGN KEY (tenant_id, endpoint_id) REFERENCES channel_endpoints (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE contacts           ADD CONSTRAINT contacts_org_tenant_fk           FOREIGN KEY (tenant_id, organization_id) REFERENCES organizations (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_tenant_pack_tenant_fk      FOREIGN KEY (tenant_id, tenant_pack_id)  REFERENCES tenant_packs (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_contact_tenant_fk          FOREIGN KEY (tenant_id, contact_id)      REFERENCES contacts (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_org_tenant_fk              FOREIGN KEY (tenant_id, organization_id) REFERENCES organizations (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_queue_tenant_fk            FOREIGN KEY (tenant_id, queue_id)        REFERENCES queues (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_assignee_tenant_fk         FOREIGN KEY (tenant_id, assigned_to)     REFERENCES staff_users (tenant_id, id);
ALTER TABLE cases              ADD CONSTRAINT cases_endpoint_source_fk         FOREIGN KEY (tenant_id, channel, endpoint_id) REFERENCES channel_endpoints (tenant_id, channel, id);
ALTER TABLE messages           ADD CONSTRAINT messages_case_tenant_fk          FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE messages           ADD CONSTRAINT messages_case_channel_fk         FOREIGN KEY (tenant_id, case_id, channel) REFERENCES cases (tenant_id, id, channel);
ALTER TABLE messages           ADD CONSTRAINT messages_case_source_fk          FOREIGN KEY (tenant_id, case_id, channel, endpoint_id) REFERENCES cases (tenant_id, id, channel, endpoint_id);
ALTER TABLE documents          ADD CONSTRAINT documents_case_tenant_fk         FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE documents          ADD CONSTRAINT documents_message_case_fk        FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE ai_runs            ADD CONSTRAINT ai_runs_case_tenant_fk           FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE ai_runs            ADD CONSTRAINT ai_runs_message_tenant_fk        FOREIGN KEY (tenant_id, message_id)      REFERENCES messages (tenant_id, id);
ALTER TABLE ai_runs            ADD CONSTRAINT ai_runs_document_tenant_fk       FOREIGN KEY (tenant_id, document_id)     REFERENCES documents (tenant_id, id);
ALTER TABLE classifications    ADD CONSTRAINT classifications_case_tenant_fk   FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE classifications    ADD CONSTRAINT classifications_case_pack_fk     FOREIGN KEY (tenant_id, case_id, pack_id) REFERENCES cases (tenant_id, id, pack_id);
ALTER TABLE classifications    ADD CONSTRAINT classifications_msg_case_fk      FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE classifications    ADD CONSTRAINT classifications_ai_run_tenant_fk FOREIGN KEY (tenant_id, ai_run_id)       REFERENCES ai_runs (tenant_id, id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_case_tenant_fk       FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_case_pack_fk         FOREIGN KEY (tenant_id, case_id, pack_id) REFERENCES cases (tenant_id, id, pack_id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_msg_case_fk          FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_doc_case_fk          FOREIGN KEY (tenant_id, case_id, document_id) REFERENCES documents (tenant_id, case_id, id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_ai_run_tenant_fk     FOREIGN KEY (tenant_id, ai_run_id)       REFERENCES ai_runs (tenant_id, id);
ALTER TABLE extractions        ADD CONSTRAINT extractions_corrector_tenant_fk  FOREIGN KEY (tenant_id, corrected_by)    REFERENCES staff_users (tenant_id, id);
ALTER TABLE knowledge_chunks   ADD CONSTRAINT knowledge_chunks_doc_tenant_fk   FOREIGN KEY (tenant_id, document_id)     REFERENCES knowledge_documents (tenant_id, id);
ALTER TABLE policy_decisions   ADD CONSTRAINT policy_decisions_case_tenant_fk  FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE policy_decisions   ADD CONSTRAINT policy_decisions_case_pack_fk    FOREIGN KEY (tenant_id, case_id, pack_id) REFERENCES cases (tenant_id, id, pack_id);
ALTER TABLE policy_decisions   ADD CONSTRAINT policy_decisions_msg_case_fk     FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE drafts             ADD CONSTRAINT drafts_case_tenant_fk            FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE drafts             ADD CONSTRAINT drafts_msg_case_fk               FOREIGN KEY (tenant_id, case_id, in_reply_to_message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE drafts             ADD CONSTRAINT drafts_sent_msg_case_fk          FOREIGN KEY (tenant_id, case_id, sent_message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE drafts             ADD CONSTRAINT drafts_decision_case_fk          FOREIGN KEY (tenant_id, case_id, policy_decision_id) REFERENCES policy_decisions (tenant_id, case_id, id);
ALTER TABLE drafts             ADD CONSTRAINT drafts_ai_run_tenant_fk          FOREIGN KEY (tenant_id, ai_run_id)       REFERENCES ai_runs (tenant_id, id);
ALTER TABLE actions            ADD CONSTRAINT actions_case_tenant_fk           FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE actions            ADD CONSTRAINT actions_msg_case_fk              FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES messages (tenant_id, case_id, id);
ALTER TABLE actions            ADD CONSTRAINT actions_decision_case_fk         FOREIGN KEY (tenant_id, case_id, policy_decision_id) REFERENCES policy_decisions (tenant_id, case_id, id);
ALTER TABLE actions            ADD CONSTRAINT actions_approver_tenant_fk       FOREIGN KEY (tenant_id, approved_by)     REFERENCES staff_users (tenant_id, id);
ALTER TABLE approval_requests  ADD CONSTRAINT approval_requests_case_tenant_fk FOREIGN KEY (tenant_id, case_id)         REFERENCES cases (tenant_id, id);
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_req_tenant_fk FOREIGN KEY (tenant_id, request_id)      REFERENCES approval_requests (tenant_id, id);
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_rev_tenant_fk FOREIGN KEY (tenant_id, reviewer_id)     REFERENCES staff_users (tenant_id, id);
ALTER TABLE dead_letters       ADD CONSTRAINT dead_letters_resolver_tenant_fk  FOREIGN KEY (tenant_id, resolved_by)     REFERENCES staff_users (tenant_id, id),
                               ADD CONSTRAINT dead_letters_resolver_requires_tenant CHECK (resolved_by IS NULL OR tenant_id IS NOT NULL);

CREATE FUNCTION enforce_case_pack_pin() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.tenant_pack_id IS NOT NULL THEN
    IF (NEW.tenant_id, NEW.tenant_pack_id, NEW.pack_id) IS DISTINCT FROM
       (OLD.tenant_id, OLD.tenant_pack_id, OLD.pack_id) THEN
      RAISE EXCEPTION 'case pack is already pinned';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.tenant_pack_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.tenant_packs tp
     WHERE tp.tenant_id = NEW.tenant_id AND tp.id = NEW.tenant_pack_id
       AND tp.pack_id = NEW.pack_id AND tp.enabled
  ) THEN
    RAISE EXCEPTION 'case requires an enabled tenant pack matching the pinned version';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER cases_enforce_pack_pin BEFORE INSERT OR UPDATE OF tenant_id, tenant_pack_id, pack_id ON cases
  FOR EACH ROW EXECUTE FUNCTION enforce_case_pack_pin();

-- =====================================================================
-- D + E. ACTIONS: payload hash, frozen payloads, state machine, approval binding
-- =====================================================================
ALTER TABLE actions
  ADD COLUMN payload_hash     text,
  ADD COLUMN attempts         integer NOT NULL DEFAULT 0,
  ADD COLUMN claimed_by       text,          -- worker / n8n execution id holding the lease
  ADD COLUMN lease_expires_at timestamptz,   -- reconciler picks up 'executing' rows past this
  ADD COLUMN external_ref     text;          -- id returned by Shopify/HubSpot/QuickBooks…

ALTER TABLE actions DROP CONSTRAINT actions_status_check;
ALTER TABLE actions ADD  CONSTRAINT actions_status_check CHECK (status IN
  ('proposed','pending_approval','approved','executing','executed','unknown_outcome','failed','rejected','cancelled'));
ALTER TABLE actions DROP CONSTRAINT money_requires_approver;
ALTER TABLE actions ADD  CONSTRAINT money_requires_approver CHECK (
  NOT (risk_level = 'money' AND status IN ('approved','executing','executed','unknown_outcome') AND approved_by IS NULL));

ALTER TABLE drafts ADD COLUMN payload_hash text;
ALTER TABLE drafts DROP CONSTRAINT drafts_status_check;
ALTER TABLE drafts ADD  CONSTRAINT drafts_status_check CHECK (status IN
  ('pending','approved','rejected','sent','send_failed','superseded'));

ALTER TABLE approval_requests ADD COLUMN payload_hash text;   -- captured at request time

CREATE OR REPLACE FUNCTION action_payload_hash(a actions) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(concat_ws('|',
           a.tenant_id::text, a.case_id::text, a.tool_key, a.risk_level,
           coalesce(a.amount::text, ''), coalesce(a.currency::text, ''), a.input::text), 'UTF8')), 'hex')
$$;

CREATE OR REPLACE FUNCTION draft_payload_hash(d drafts) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(concat_ws('|',
           d.tenant_id::text, d.case_id::text, d.channel, d.recipient,
           coalesce(d.subject, ''), d.body), 'UTF8')), 'hex')
$$;

-- The one place that decides whether an action may run.
CREATE OR REPLACE FUNCTION enforce_action_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  registry_risk  text;
  req            approval_requests%ROWTYPE;
  draft_status   text;
BEGIN
  NEW.payload_hash := action_payload_hash(NEW);

  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('proposed','pending_approval') THEN
      RAISE EXCEPTION 'actions must be created as proposed or pending_approval, not %', NEW.status;
    END IF;
    SELECT risk_level INTO registry_risk FROM tools WHERE key = NEW.tool_key AND active;
    IF registry_risk IS NULL THEN
      RAISE EXCEPTION 'tool % is not registered or inactive', NEW.tool_key;
    END IF;
    IF NEW.risk_level <> registry_risk THEN
      RAISE EXCEPTION 'risk_level % does not match registry (%) for tool %', NEW.risk_level, registry_risk, NEW.tool_key;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM cases c JOIN pack_tools pt ON pt.pack_id = c.pack_id AND pt.tool_key = NEW.tool_key
                   WHERE c.id = NEW.case_id AND c.tenant_id = NEW.tenant_id) THEN
      RAISE EXCEPTION 'tool % is not allowed for this case''s pack', NEW.tool_key;
    END IF;
    RETURN NEW;
  END IF;

  -- ---- UPDATE ----
  IF OLD.status IN ('executed','rejected','cancelled') THEN
    RAISE EXCEPTION 'action % is final (%) and cannot change', OLD.id, OLD.status;
  END IF;

  -- Payload is frozen once it leaves 'proposed': an approval covers exactly what was shown.
  IF OLD.status <> 'proposed' AND NEW.payload_hash IS DISTINCT FROM OLD.payload_hash THEN
    RAISE EXCEPTION 'action % payload is frozen after approval is requested; create a new action instead', OLD.id;
  END IF;

  IF NEW.status <> OLD.status THEN
    IF NOT (
         (OLD.status = 'proposed'         AND NEW.status IN ('pending_approval','executing','rejected','cancelled'))
      OR (OLD.status = 'pending_approval' AND NEW.status IN ('approved','rejected','cancelled'))
      OR (OLD.status = 'approved'         AND NEW.status IN ('executing','cancelled'))
      OR (OLD.status = 'executing'        AND NEW.status IN ('executed','failed','unknown_outcome'))
      OR (OLD.status = 'unknown_outcome'  AND NEW.status IN ('executed','failed'))      -- after reconciliation
      OR (OLD.status = 'failed'           AND NEW.status IN ('executing','cancelled'))  -- retry
    ) THEN
      RAISE EXCEPTION 'illegal action transition % -> %', OLD.status, NEW.status;
    END IF;

    IF NEW.status = 'executing' THEN
      NEW.attempts := OLD.attempts + 1;
      IF NEW.claimed_by IS NULL OR NEW.lease_expires_at IS NULL THEN
        RAISE EXCEPTION 'executing requires claimed_by and lease_expires_at';
      END IF;
    END IF;
  END IF;

  -- Approval binding: applies whenever the action is (about to be) approved or run.
  IF NEW.status IN ('approved','executing','executed') THEN
    SELECT * INTO req FROM approval_requests
     WHERE subject_type = 'action' AND subject_id = NEW.id
     ORDER BY created_at DESC LIMIT 1;

    IF NEW.risk_level = 'money' AND req.id IS NULL THEN
      RAISE EXCEPTION 'money action % has no approval request', NEW.id;
    END IF;

    IF req.id IS NOT NULL THEN
      IF req.status <> 'approved' THEN
        RAISE EXCEPTION 'approval request for action % is %, not approved', NEW.id, req.status;
      END IF;
      IF req.payload_hash IS DISTINCT FROM NEW.payload_hash THEN
        RAISE EXCEPTION 'approval for action % was given for a different payload', NEW.id;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM approval_decisions d
                      WHERE d.request_id = req.id AND d.decision = 'approved'
                        AND d.reviewer_id = NEW.approved_by) THEN
        RAISE EXCEPTION 'approved_by must be the reviewer who approved request %', req.id;
      END IF;
    END IF;
  END IF;

  -- Outbound messages may only send an approved draft.
  IF NEW.status = 'executing' AND NEW.tool_key = 'send_reply' THEN
    SELECT status INTO draft_status FROM drafts
     WHERE id = (NEW.input->>'draft_id')::uuid AND tenant_id = NEW.tenant_id AND case_id = NEW.case_id;
    IF draft_status IS DISTINCT FROM 'approved' THEN
      RAISE EXCEPTION 'send_reply requires input.draft_id of an approved draft in the same case (got %)', coalesce(draft_status,'none');
    END IF;
  END IF;

  RETURN NEW;
END $$;

CREATE TRIGGER actions_enforce_rules BEFORE INSERT OR UPDATE ON actions
  FOR EACH ROW EXECUTE FUNCTION enforce_action_rules();

-- Drafts: hash, and content frozen once it leaves 'pending'.
CREATE OR REPLACE FUNCTION enforce_draft_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.payload_hash := draft_payload_hash(NEW);
  IF TG_OP = 'UPDATE' THEN
    IF OLD.status <> 'pending' AND NEW.payload_hash IS DISTINCT FROM OLD.payload_hash THEN
      RAISE EXCEPTION 'draft % content is frozen once %; create a new draft', OLD.id, OLD.status;
    END IF;
    IF OLD.status IN ('sent','rejected','superseded') AND NEW.status <> OLD.status THEN
      RAISE EXCEPTION 'draft % is final (%)', OLD.id, OLD.status;
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER drafts_enforce_rules BEFORE INSERT OR UPDATE ON drafts
  FOR EACH ROW EXECUTE FUNCTION enforce_draft_rules();

-- Approval requests capture the exact payload hash of their subject, same tenant + case.
CREATE OR REPLACE FUNCTION capture_approval_payload() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE h text;
BEGIN
  IF NEW.subject_type = 'action' THEN
    SELECT payload_hash INTO h FROM actions WHERE id = NEW.subject_id AND tenant_id = NEW.tenant_id AND case_id = NEW.case_id;
  ELSIF NEW.subject_type = 'draft' THEN
    SELECT payload_hash INTO h FROM drafts  WHERE id = NEW.subject_id AND tenant_id = NEW.tenant_id AND case_id = NEW.case_id;
  ELSE
    SELECT encode(sha256(convert_to(data::text, 'UTF8')), 'hex') INTO h
      FROM extractions WHERE id = NEW.subject_id AND tenant_id = NEW.tenant_id AND case_id = NEW.case_id;
  END IF;
  IF h IS NULL THEN
    RAISE EXCEPTION 'approval subject % % not found in this tenant/case', NEW.subject_type, NEW.subject_id;
  END IF;
  NEW.payload_hash := h;
  RETURN NEW;
END $$;

CREATE TRIGGER approval_requests_capture_payload BEFORE INSERT ON approval_requests
  FOR EACH ROW EXECUTE FUNCTION capture_approval_payload();

-- Fix: fail closed when the reviewer is not visible/found (e.g. other tenant under RLS).
CREATE OR REPLACE FUNCTION check_approval_reviewer() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  req   approval_requests%ROWTYPE;
  staff staff_users%ROWTYPE;
BEGIN
  SELECT * INTO req   FROM approval_requests WHERE id = NEW.request_id AND tenant_id = NEW.tenant_id;
  IF req.id IS NULL THEN
    RAISE EXCEPTION 'approval request % not found in this tenant', NEW.request_id;
  END IF;
  SELECT * INTO staff FROM staff_users WHERE id = NEW.reviewer_id AND tenant_id = NEW.tenant_id;
  IF staff.id IS NULL THEN
    RAISE EXCEPTION 'reviewer % not found in this tenant', NEW.reviewer_id;
  END IF;
  IF req.status <> 'pending' THEN
    RAISE EXCEPTION 'approval request % is already %', req.id, req.status;
  END IF;
  IF NOT staff.active THEN
    RAISE EXCEPTION 'reviewer % is inactive', staff.id;
  END IF;
  IF NOT (staff.role = 'admin' OR staff.role = req.required_role
          OR (req.required_role = 'agent' AND staff.role IN ('manager','finance'))) THEN
    RAISE EXCEPTION 'role % cannot satisfy required role %', staff.role, req.required_role;
  END IF;
  -- The subject must not have changed since the request was raised.
  IF req.subject_type = 'action' AND NOT EXISTS
       (SELECT 1 FROM actions a WHERE a.id = req.subject_id AND a.payload_hash = req.payload_hash) THEN
    RAISE EXCEPTION 'action changed after approval was requested';
  END IF;
  IF req.subject_type = 'draft' AND NOT EXISTS
       (SELECT 1 FROM drafts d WHERE d.id = req.subject_id AND d.payload_hash = req.payload_hash) THEN
    RAISE EXCEPTION 'draft changed after approval was requested';
  END IF;
  RETURN NEW;
END $$;

-- A decision resolves its request atomically (no app code needed, no window for mismatch).
CREATE OR REPLACE FUNCTION apply_approval_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE approval_requests
     SET status = NEW.decision, resolved_at = NEW.decided_at
   WHERE id = NEW.request_id;
  RETURN NEW;
END $$;

CREATE TRIGGER approval_decisions_apply AFTER INSERT ON approval_decisions
  FOR EACH ROW EXECUTE FUNCTION apply_approval_decision();

-- =====================================================================
-- F. TAMPER-EVIDENT AUDIT LOG (hash chain)
-- =====================================================================
ALTER TABLE audit_log
  ADD COLUMN chain_seq bigint,
  ADD COLUMN prev_hash text,
  ADD COLUMN row_hash  text;

CREATE OR REPLACE FUNCTION audit_row_hash(seq bigint, prev text, a audit_log) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(concat_ws('|',
           seq::text, prev, coalesce(a.tenant_id::text,''), coalesce(a.trace_id::text,''),
           a.actor_type, coalesce(a.actor_id,''), a.event_type, coalesce(a.entity_type,''),
           coalesce(a.entity_id::text,''), a.details::text,
           extract(epoch FROM a.created_at)::text), 'UTF8')), 'hex')
$$;

-- SECURITY DEFINER: under RLS the app role only sees its own tenant's rows, but the chain
-- must link to the true latest row across all tenants.
CREATE OR REPLACE FUNCTION audit_log_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE last_seq bigint; last_hash text;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('audit_log_chain'));   -- serialize chain appends
  SELECT chain_seq, row_hash INTO last_seq, last_hash FROM public.audit_log ORDER BY chain_seq DESC NULLS LAST LIMIT 1;
  NEW.created_at := clock_timestamp();
  NEW.chain_seq  := coalesce(last_seq, 0) + 1;
  NEW.prev_hash  := coalesce(last_hash, 'GENESIS');
  NEW.row_hash   := public.audit_row_hash(NEW.chain_seq, NEW.prev_hash, NEW);
  RETURN NEW;
END $$;

-- Backfill rows written before this migration (e.g. by an earlier seed), then lock it down.
ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;
DO $$
DECLARE r audit_log%ROWTYPE; seq bigint := 0; prev text := 'GENESIS';
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
    seq := seq + 1;
    UPDATE audit_log SET chain_seq = seq, prev_hash = prev, row_hash = audit_row_hash(seq, prev, r) WHERE id = r.id;
    prev := audit_row_hash(seq, prev, r);
  END LOOP;
END $$;
ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;

ALTER TABLE audit_log ALTER COLUMN chain_seq SET NOT NULL,
                      ALTER COLUMN prev_hash SET NOT NULL,
                      ALTER COLUMN row_hash  SET NOT NULL;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_chain_seq_key UNIQUE (chain_seq);

CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_chain();
REVOKE ALL ON FUNCTION audit_log_chain() FROM PUBLIC;

-- Returns 'OK' or the first broken link. Run daily (wf_audit_verify) and anchor the
-- latest row_hash outside the DB (Slack message / MinIO object-lock bucket).
CREATE OR REPLACE FUNCTION verify_audit_chain() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE r audit_log%ROWTYPE; prev text := 'GENESIS'; expected_seq bigint := 0;
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY chain_seq LOOP
    expected_seq := expected_seq + 1;
    IF r.chain_seq <> expected_seq THEN RETURN format('BROKEN: missing chain_seq %s', expected_seq); END IF;
    IF r.prev_hash <> prev THEN RETURN format('BROKEN: prev_hash mismatch at chain_seq %s', r.chain_seq); END IF;
    IF r.row_hash <> audit_row_hash(r.chain_seq, r.prev_hash, r) THEN
      RETURN format('BROKEN: row %s was modified', r.chain_seq);
    END IF;
    prev := r.row_hash;
  END LOOP;
  RETURN format('OK (%s rows, head %s)', expected_seq, left(prev, 16));
END $$;

-- =====================================================================
-- B + C. ROLES, PRIVILEGES, ROW-LEVEL SECURITY
-- =====================================================================
-- The bootstrap step creates the group and login roles before running this migration.

-- Start from zero on tables. (Function EXECUTE stays default: extension functions such as
-- gen_random_uuid() and vector operators must remain callable. Privileged functions are
-- revoked individually below.)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA demo FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app_core, app_config_loader, app_reporting;

-- app_core: the core service (the only writer of operational data).
GRANT SELECT ON tenants, tenant_settings, staff_users, queues, tenant_packs, channel_endpoints,
                packs, pack_intents, tools, pack_tools TO app_core;                       -- config: READ ONLY
GRANT SELECT, INSERT, UPDATE ON organizations, contacts, cases, messages, documents, ai_runs,
                classifications, extractions, knowledge_documents, knowledge_chunks,
                policy_decisions, drafts, actions, approval_requests, approval_decisions,
                dead_letters, eval_cases, eval_runs, eval_results TO app_core;
GRANT SELECT, INSERT ON audit_log TO app_core;                                            -- never UPDATE/DELETE
-- Note: no DELETE anywhere. GDPR = anonymize via UPDATE.

-- app_config_loader: scripts/load_packs + admin tooling. Changes config, never operational data.
GRANT SELECT, INSERT, UPDATE ON tenants, tenant_settings, staff_users, queues, tenant_packs,
                channel_endpoints, packs, pack_intents, tools, pack_tools,
                knowledge_documents, knowledge_chunks, eval_cases TO app_config_loader;
ALTER TABLE ingress_credentials ENABLE ROW LEVEL SECURITY;
CREATE POLICY ingress_credentials_manage ON ingress_credentials TO app_config_loader USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (revoked_at) ON ingress_credentials TO app_config_loader;
GRANT SELECT, INSERT ON audit_log TO app_config_loader;

-- app_n8n_mock: n8n's mock tool sub-workflows. Demo schema only; zero access to engine tables.
GRANT USAGE ON SCHEMA demo TO app_n8n_mock;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA demo TO app_n8n_mock;

-- app_reporting: Metabase. Views only (internal, cross-tenant by design).
GRANT SELECT ON v_case_outcomes, v_daily_kpis, v_daily_ai_cost, v_pending_approvals TO app_reporting;

-- Tenant context for RLS. Unset → NULL → no rows (fail closed).
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;
GRANT EXECUTE ON FUNCTION app_current_tenant() TO app_core, app_config_loader, app_reporting;

-- Ingest must resolve tenant from an inbound address BEFORE tenant context exists.
-- SECURITY DEFINER exposes exactly this lookup and nothing else.
CREATE FUNCTION resolve_channel_endpoint(p_token_digest bytea)
RETURNS TABLE (tenant_id uuid, tenant_pack_id uuid, endpoint_id uuid, pack_id uuid, channel text, address text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT e.tenant_id, e.tenant_pack_id, e.id, tp.pack_id, e.channel, e.address
    FROM public.ingress_credentials credential
    JOIN public.channel_endpoints e ON e.tenant_id = credential.tenant_id AND e.id = credential.endpoint_id
    JOIN public.tenants t ON t.id = e.tenant_id
    LEFT JOIN public.tenant_packs tp ON tp.tenant_id = e.tenant_id AND tp.id = e.tenant_pack_id
   WHERE credential.token_digest = p_token_digest AND credential.revoked_at IS NULL
     AND credential.expires_at > statement_timestamp() AND e.active AND t.status = 'active'
     AND (e.tenant_pack_id IS NULL OR tp.enabled)
$$;
REVOKE ALL ON FUNCTION resolve_channel_endpoint(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_channel_endpoint(bytea) TO app_core;

-- Enable RLS + policies on every tenant-scoped table.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenant_settings','staff_users','queues','tenant_packs','channel_endpoints',
                           'organizations','contacts','cases','messages','documents','ai_runs',
                           'classifications','extractions','knowledge_documents','knowledge_chunks',
                           'policy_decisions','drafts','actions','approval_requests','approval_decisions']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I TO app_core
                      USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())', t);
    EXECUTE format('CREATE POLICY config_loader_all ON %I TO app_config_loader USING (true) WITH CHECK (true)', t);
  END LOOP;
END $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation  ON tenants TO app_core          USING (id = app_current_tenant());
CREATE POLICY config_loader_all ON tenants TO app_config_loader USING (true) WITH CHECK (true);

-- Rows that may legitimately have no tenant (unresolvable input, system events).
ALTER TABLE dead_letters ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dead_letters TO app_core
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id IS NULL OR tenant_id = app_current_tenant());

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_log TO app_core
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id IS NULL OR tenant_id = app_current_tenant());
CREATE POLICY config_loader_insert ON audit_log FOR INSERT TO app_config_loader WITH CHECK (true);
CREATE POLICY config_loader_read   ON audit_log FOR SELECT TO app_config_loader USING (true);

COMMIT;
