-- =====================================================================
-- 001_init.sql — Multi-Purpose AI Operations Platform
-- PostgreSQL 16+ with pgvector. Forward-only: never edit after it is applied.
-- Place at: db/migrations/001_init.sql
--
-- Schema "public" = the generic engine (knows nothing about orders or invoices).
-- Schema "demo"   = mock external systems (shop, CRM, accounting, calendar)
--                   used by the `mock` tool adapters until real integrations exist.
-- =====================================================================

BEGIN;

-- The bootstrap step installs pgcrypto, citext and vector before running this migration.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- =====================================================================
-- 1. TENANCY & STAFF
-- =====================================================================
CREATE TABLE tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,50}$'),
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','archived')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Engine-wide settings per tenant. Pack-specific settings live in tenant_packs.config.
CREATE TABLE tenant_settings (
  tenant_id              uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  monthly_ai_budget_usd  numeric(10,2) NOT NULL DEFAULT 50 CHECK (monthly_ai_budget_usd >= 0),
  primary_model          text NOT NULL DEFAULT 'default-fast',     -- LiteLLM model alias
  strong_model           text NOT NULL DEFAULT 'default-strong',   -- drafting / documents
  fallback_model         text NOT NULL DEFAULT 'default-fallback',
  default_language       text NOT NULL DEFAULT 'en',
  timezone               text NOT NULL DEFAULT 'UTC',
  brand_voice            text,
  approval_sla_minutes   integer NOT NULL DEFAULT 60 CHECK (approval_sla_minutes > 0),
  data_retention_days    integer NOT NULL DEFAULT 365 CHECK (data_retention_days > 0),
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE staff_users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email          citext NOT NULL,
  name           text NOT NULL,
  role           text NOT NULL CHECK (role IN ('agent','manager','finance','admin')),
  slack_user_id  text,
  active         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);

CREATE TABLE queues (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key             text NOT NULL,             -- 'customer_support','sales','accounts_payable','escalations'
  name            text NOT NULL,
  notify_channel  text,                      -- Slack channel id
  UNIQUE (tenant_id, key)
);

-- =====================================================================
-- 2. PACKS & TOOLS (synced from /packs by scripts/load_packs)
-- =====================================================================
CREATE TABLE packs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key          text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{2,50}$'),
  version      integer NOT NULL CHECK (version > 0),
  name         text NOT NULL,
  description  text,
  definition   jsonb NOT NULL,              -- full parsed pack (pack.yaml + rules + tools refs)
  checksum     text NOT NULL,               -- sha256 of the pack folder
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated')),
  loaded_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (key, version),
  UNIQUE (id, key)                          -- lets tenant_packs enforce key/id consistency
);

CREATE TABLE pack_intents (
  pack_id           uuid NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
  intent_key        text NOT NULL CHECK (intent_key ~ '^[a-z][a-z0-9_]{1,50}$'),
  description       text NOT NULL,
  default_priority  text NOT NULL DEFAULT 'normal' CHECK (default_priority IN ('low','normal','high','urgent')),
  PRIMARY KEY (pack_id, intent_key)
);

-- Global tool registry. Implementation: services/core/app/tools/<key>/{mock,real}.py
CREATE TABLE tools (
  key                         text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{2,60}$'),
  name                        text NOT NULL,
  description                 text NOT NULL,
  risk_level                  text NOT NULL CHECK (risk_level IN ('read','write','external_comm','money')),
  requires_verified_identity  boolean NOT NULL DEFAULT false,
  input_schema                jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_schema               jsonb NOT NULL DEFAULT '{}'::jsonb,
  timeout_ms                  integer NOT NULL DEFAULT 10000 CHECK (timeout_ms BETWEEN 100 AND 120000),
  version                     integer NOT NULL DEFAULT 1,
  active                      boolean NOT NULL DEFAULT true
);

-- Which tools a pack may use, and who must approve them.
CREATE TABLE pack_tools (
  pack_id                uuid NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
  tool_key               text NOT NULL REFERENCES tools(key),
  approval_role          text CHECK (approval_role IN ('agent','manager','finance','admin')),
  approval_above_amount  numeric(12,2) CHECK (approval_above_amount >= 0),
  config                 jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (pack_id, tool_key)
);

-- Packs a tenant has switched on (pinned to a version).
CREATE TABLE tenant_packs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  pack_key    text NOT NULL,
  pack_id     uuid NOT NULL,
  enabled     boolean NOT NULL DEFAULT true,
  config      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"adapters":{"order_lookup":"mock"},"thresholds":{...},"rules":[...]}
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, pack_key),
  FOREIGN KEY (pack_id, pack_key) REFERENCES packs(id, key)
);

-- Inbound addresses → pack. tenant_pack_id NULL = shared inbox, AI router picks the pack.
CREATE TABLE channel_endpoints (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_pack_id  uuid REFERENCES tenant_packs(id) ON DELETE SET NULL,
  channel         text NOT NULL CHECK (channel IN ('email','web_chat','web_form','api','whatsapp','slack')),
  address         text NOT NULL,             -- email address, form id, chat widget id, API key id
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, address)                  -- globally unique so ingest can resolve the tenant
);

-- =====================================================================
-- 3. CONTACTS & ORGANIZATIONS (generic people/companies)
-- =====================================================================
CREATE TABLE organizations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          text NOT NULL,
  domain        citext,
  external_ids  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"hubspot":"123","quickbooks_vendor":"45"}
  attributes    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- enrichment: industry, size, country…
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, domain)
);

CREATE TABLE contacts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  organization_id  uuid REFERENCES organizations(id) ON DELETE SET NULL,
  email            citext,
  full_name        text,
  phone            text,
  locale           text,
  kind             text NOT NULL DEFAULT 'unknown' CHECK (kind IN ('customer','lead','vendor','employee','unknown')),
  external_ids     jsonb NOT NULL DEFAULT '{}'::jsonb,
  anonymized_at    timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);

-- =====================================================================
-- 4. CASES, MESSAGES, DOCUMENTS
-- =====================================================================
-- A case is one unit of work: a support conversation, a lead, an invoice.
CREATE TABLE cases (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_pack_id      uuid REFERENCES tenant_packs(id),      -- null until routed
  pack_id             uuid REFERENCES packs(id),             -- exact pack version used
  contact_id          uuid REFERENCES contacts(id),
  organization_id     uuid REFERENCES organizations(id),
  channel             text NOT NULL CHECK (channel IN ('email','web_chat','web_form','api','whatsapp','slack')),
  endpoint_id         uuid REFERENCES channel_endpoints(id),
  external_thread_id  text,
  status              text NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new','in_progress','waiting_contact','waiting_staff','escalated','resolved','closed','ignored')),
  priority            text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),
  queue_id            uuid REFERENCES queues(id),
  assigned_to         uuid REFERENCES staff_users(id),
  identity_verified   boolean NOT NULL DEFAULT false,
  subject             text,
  summary             text,                                  -- AI summary for reviewers
  outcome             jsonb NOT NULL DEFAULT '{}'::jsonb,    -- e.g. {"lead_score":82} {"accounting_bill_id":"B-77"}
  sla_due_at          timestamptz,
  last_activity_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CHECK ((tenant_pack_id IS NULL) = (pack_id IS NULL))
);
CREATE UNIQUE INDEX cases_source_thread_key ON cases (tenant_id, channel, endpoint_id, external_thread_id)
  NULLS NOT DISTINCT WHERE external_thread_id IS NOT NULL;
CREATE INDEX cases_status_idx ON cases (tenant_id, status, last_activity_at DESC);
CREATE INDEX cases_pack_idx   ON cases (tenant_id, pack_id, created_at);

CREATE TABLE messages (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id             uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  endpoint_id         uuid,
  trace_id            uuid NOT NULL,
  idempotency_key     text NOT NULL,
  direction           text NOT NULL CHECK (direction IN ('inbound','outbound')),
  channel             text NOT NULL CHECK (channel IN ('email','web_chat','web_form','api','whatsapp','slack')),
  external_message_id text,
  sender_address      text,
  subject             text,
  body_text           text,              -- raw; restricted; nulled by GDPR delete
  body_redacted       text,              -- what the LLM sees
  form_fields         jsonb NOT NULL DEFAULT '{}'::jsonb,
  processing_status   text NOT NULL DEFAULT 'received'
                      CHECK (processing_status IN ('received','processing','processed','failed','dead_lettered','skipped')),
  attempt_count       integer NOT NULL DEFAULT 0,
  last_error          text,
  received_at         timestamptz NOT NULL DEFAULT now(),
  processed_at        timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT messages_inbound_requires_endpoint CHECK (direction <> 'inbound' OR endpoint_id IS NOT NULL),
  CONSTRAINT messages_source_idempotency_key UNIQUE NULLS NOT DISTINCT (tenant_id, endpoint_id, idempotency_key)
);
CREATE INDEX messages_case_idx    ON messages (case_id, received_at);
CREATE INDEX messages_trace_idx   ON messages (trace_id);
CREATE INDEX messages_pending_idx ON messages (tenant_id, processing_status) WHERE processing_status IN ('received','failed');

-- Files live in MinIO/S3; this is the metadata.
CREATE TABLE documents (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id            uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  message_id         uuid REFERENCES messages(id) ON DELETE SET NULL,
  storage_bucket     text NOT NULL,
  storage_key        text NOT NULL,
  filename           text NOT NULL,
  mime_type          text NOT NULL,
  size_bytes         bigint NOT NULL CHECK (size_bytes >= 0),
  sha256             text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  page_count         integer,
  doc_type           text,                 -- set by classification: 'invoice','credit_note','receipt',…
  processing_status  text NOT NULL DEFAULT 'stored'
                     CHECK (processing_status IN ('stored','extracting','extracted','failed','deleted')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (storage_bucket, storage_key)
);
-- Not unique: the same file arriving twice is a business signal (duplicate invoice), not an error.
CREATE INDEX documents_sha_idx  ON documents (tenant_id, sha256);
CREATE INDEX documents_case_idx ON documents (case_id);

-- =====================================================================
-- 5. AI LAYER
-- =====================================================================
CREATE TABLE ai_runs (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id            uuid REFERENCES cases(id) ON DELETE SET NULL,
  message_id         uuid REFERENCES messages(id) ON DELETE SET NULL,
  document_id        uuid REFERENCES documents(id) ON DELETE SET NULL,
  trace_id           uuid NOT NULL,
  step               text NOT NULL CHECK (step IN ('route','guardrail_in','classify','extract','ocr','agent',
                                                   'rag_query','draft','guardrail_out','summarize','embed','eval')),
  provider           text NOT NULL,
  model              text NOT NULL,
  prompt_version     text,
  used_fallback      boolean NOT NULL DEFAULT false,
  input_tokens       integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens      integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cost_usd           numeric(12,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  latency_ms         integer CHECK (latency_ms >= 0),
  status             text NOT NULL CHECK (status IN ('success','schema_invalid','timeout','error','budget_blocked')),
  output             jsonb,
  error              text,
  langfuse_trace_id  text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_runs_trace_idx ON ai_runs (trace_id);
CREATE INDEX ai_runs_cost_idx  ON ai_runs (tenant_id, created_at);

CREATE TABLE classifications (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id            uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  message_id         uuid REFERENCES messages(id) ON DELETE CASCADE,
  pack_id            uuid NOT NULL,
  intent_key         text NOT NULL,
  ai_run_id          uuid REFERENCES ai_runs(id) ON DELETE SET NULL,   -- null for keyword rules
  method             text NOT NULL CHECK (method IN ('rules','llm','human')),
  secondary_intents  text[] NOT NULL DEFAULT '{}',
  priority           text NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
  sentiment          text CHECK (sentiment IN ('very_negative','negative','neutral','positive')),
  language           text,
  confidence         numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  flags              text[] NOT NULL DEFAULT '{}',   -- 'prompt_injection','legal_threat','chargeback','spam',…
  created_at         timestamptz NOT NULL DEFAULT now(),
  -- An intent must exist in the pack that classified it. No more hard-coded intent lists.
  FOREIGN KEY (pack_id, intent_key) REFERENCES pack_intents(pack_id, intent_key)
);
CREATE INDEX classifications_case_idx   ON classifications (case_id, created_at DESC);
CREATE INDEX classifications_intent_idx ON classifications (tenant_id, pack_id, intent_key);

-- Structured data pulled from a message or document, per the pack's schema.
CREATE TABLE extractions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id             uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  message_id          uuid REFERENCES messages(id) ON DELETE CASCADE,
  document_id         uuid REFERENCES documents(id) ON DELETE CASCADE,
  pack_id             uuid NOT NULL REFERENCES packs(id),
  schema_key          text NOT NULL,          -- 'invoice','lead','order_reference'
  schema_version      integer NOT NULL,
  data                jsonb NOT NULL,         -- {"invoice_number":"INV-881","total_amount":1240.00,…}
  field_confidence    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"invoice_number":0.99,"total_amount":0.97}
  overall_confidence  numeric(4,3) CHECK (overall_confidence BETWEEN 0 AND 1),
  validation_errors   jsonb NOT NULL DEFAULT '[]'::jsonb,
  status              text NOT NULL DEFAULT 'extracted'
                      CHECK (status IN ('extracted','validated','needs_review','corrected')),
  corrected_by        uuid REFERENCES staff_users(id),
  ai_run_id           uuid REFERENCES ai_runs(id) ON DELETE SET NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (message_id IS NOT NULL OR document_id IS NOT NULL),
  CHECK (status <> 'corrected' OR corrected_by IS NOT NULL)
);
CREATE INDEX extractions_case_idx ON extractions (case_id, created_at DESC);

-- =====================================================================
-- 6. KNOWLEDGE BASE (RAG)
-- =====================================================================
CREATE TABLE knowledge_documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  pack_key    text,                 -- null = shared across all of the tenant's packs
  title       text NOT NULL,
  doc_type    text NOT NULL CHECK (doc_type IN ('policy','faq','product','pricing','procedure','other')),
  source_uri  text,
  version     integer NOT NULL DEFAULT 1,
  checksum    text NOT NULL,        -- skip re-embedding if unchanged
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','archived')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, title, version)
);

CREATE TABLE knowledge_chunks (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id      uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  pack_key         text,            -- copied from document for fast filtering
  chunk_index      integer NOT NULL,
  content          text NOT NULL,
  token_count      integer,
  embedding        vector(1536),
  embedding_model  text,
  metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, chunk_index)
);
CREATE INDEX knowledge_chunks_scope_idx     ON knowledge_chunks (tenant_id, pack_key);
CREATE INDEX knowledge_chunks_embedding_idx ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- =====================================================================
-- 7. DECISIONS, DRAFTS, ACTIONS, APPROVALS
-- =====================================================================
CREATE TABLE policy_decisions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id         uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  message_id      uuid REFERENCES messages(id) ON DELETE CASCADE,
  pack_id         uuid NOT NULL REFERENCES packs(id),
  trace_id        uuid NOT NULL,
  decision        text NOT NULL CHECK (decision IN ('auto','needs_approval','escalate','ignore')),
  rule_id         text NOT NULL,           -- 'G4_LOW_CONFIDENCE', 'INV_PO_VARIANCE', …
  rule_source     text NOT NULL CHECK (rule_source IN ('global','pack','tenant','default')),
  reason          text NOT NULL,
  required_role   text CHECK (required_role IN ('agent','manager','finance','admin')),
  allowed_tools   text[] NOT NULL DEFAULT '{}',
  policy_version  text NOT NULL,           -- e.g. 'engine:1.0+invoice_processing:1'
  inputs          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- snapshot of the decision context
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX policy_decisions_case_idx ON policy_decisions (case_id, created_at DESC);

-- Any outbound communication (email reply, chat reply, booking confirmation).
CREATE TABLE drafts (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id               uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id                 uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  in_reply_to_message_id  uuid REFERENCES messages(id) ON DELETE CASCADE,
  policy_decision_id      uuid REFERENCES policy_decisions(id),
  ai_run_id               uuid REFERENCES ai_runs(id) ON DELETE SET NULL,
  channel                 text NOT NULL CHECK (channel IN ('email','web_chat','web_form','api','whatsapp','slack')),
  recipient               text NOT NULL,
  subject                 text,
  body                    text NOT NULL,
  citations               jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{"type":"chunk","id":"…"},{"type":"tool","action_id":"…"}]
  guardrail_results       jsonb NOT NULL DEFAULT '{}'::jsonb,
  status                  text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','approved','rejected','sent','send_failed','superseded')),
  sent_message_id         uuid REFERENCES messages(id),
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);
-- At most one live draft answering a given message (prevents double replies).
CREATE UNIQUE INDEX drafts_one_live_idx ON drafts (in_reply_to_message_id)
  WHERE status IN ('pending','approved','sent');

-- Tool calls: proposed by the AI or a rule, executed only by the engine.
CREATE TABLE actions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id             uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  message_id          uuid REFERENCES messages(id) ON DELETE SET NULL,
  policy_decision_id  uuid REFERENCES policy_decisions(id),
  trace_id            uuid NOT NULL,
  idempotency_key     text NOT NULL,
  tool_key            text NOT NULL REFERENCES tools(key),
  risk_level          text NOT NULL CHECK (risk_level IN ('read','write','external_comm','money')),  -- snapshot
  adapter             text NOT NULL DEFAULT 'mock' CHECK (adapter IN ('mock','real')),
  amount              numeric(12,2),
  currency            char(3),
  input               jsonb NOT NULL DEFAULT '{}'::jsonb,
  output              jsonb,
  status              text NOT NULL CHECK (status IN ('proposed','pending_approval','approved','executed','rejected','failed','cancelled')),
  approved_by         uuid REFERENCES staff_users(id),
  error               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  executed_at         timestamptz,
  UNIQUE (tenant_id, idempotency_key),
  -- Safety net in the database itself: money never moves without a named approver.
  CONSTRAINT money_requires_approver CHECK (
    NOT (risk_level = 'money' AND status IN ('approved','executed') AND approved_by IS NULL)
  )
);
CREATE INDEX actions_case_idx  ON actions (case_id, created_at);
CREATE INDEX actions_trace_idx ON actions (trace_id);

CREATE TABLE approval_requests (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  case_id           uuid NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  subject_type      text NOT NULL CHECK (subject_type IN ('draft','action','extraction')),
  subject_id        uuid NOT NULL,
  required_role     text NOT NULL CHECK (required_role IN ('agent','manager','finance','admin')),
  reason            text NOT NULL,
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','edited','rejected','escalated','expired')),
  sla_due_at        timestamptz NOT NULL,
  slack_channel     text,
  slack_message_ts  text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  resolved_at       timestamptz
);
CREATE UNIQUE INDEX approval_requests_one_pending_idx ON approval_requests (subject_type, subject_id)
  WHERE status = 'pending';
CREATE INDEX approval_requests_sla_idx ON approval_requests (sla_due_at) WHERE status = 'pending';

CREATE TABLE approval_decisions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  request_id      uuid NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
  reviewer_id     uuid NOT NULL REFERENCES staff_users(id),
  decision        text NOT NULL CHECK (decision IN ('approved','edited','rejected','escalated')),
  edited_payload  jsonb,
  comment         text,
  channel         text NOT NULL DEFAULT 'slack' CHECK (channel IN ('slack','teams','web','api')),
  decided_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (decision <> 'edited' OR edited_payload IS NOT NULL)
);
CREATE INDEX approval_decisions_request_idx ON approval_decisions (request_id);

-- Reviewer must be active, in the same tenant, and hold a role that satisfies the request.
-- admin satisfies everything; manager and finance also satisfy 'agent'.
CREATE OR REPLACE FUNCTION check_approval_reviewer() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  req   approval_requests%ROWTYPE;
  staff staff_users%ROWTYPE;
BEGIN
  SELECT * INTO req   FROM approval_requests WHERE id = NEW.request_id;
  SELECT * INTO staff FROM staff_users       WHERE id = NEW.reviewer_id;

  IF req.status <> 'pending' THEN
    RAISE EXCEPTION 'approval request % is already %', req.id, req.status;
  END IF;
  IF staff.tenant_id <> req.tenant_id OR NEW.tenant_id <> req.tenant_id THEN
    RAISE EXCEPTION 'reviewer belongs to a different tenant';
  END IF;
  IF NOT staff.active THEN
    RAISE EXCEPTION 'reviewer % is inactive', staff.id;
  END IF;
  IF NOT (
       staff.role = 'admin'
    OR staff.role = req.required_role
    OR (req.required_role = 'agent' AND staff.role IN ('manager','finance'))
  ) THEN
    RAISE EXCEPTION 'role % cannot satisfy required role %', staff.role, req.required_role;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER approval_decisions_check_reviewer BEFORE INSERT ON approval_decisions
  FOR EACH ROW EXECUTE FUNCTION check_approval_reviewer();

-- =====================================================================
-- 8. RELIABILITY & AUDIT
-- =====================================================================
CREATE TABLE dead_letters (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid REFERENCES tenants(id) ON DELETE CASCADE,
  trace_id       uuid,
  source         text NOT NULL,          -- 'wf_case_pipeline', 'core:/understand', …
  failed_step    text,
  payload        jsonb NOT NULL,
  error          text NOT NULL,
  attempt_count  integer NOT NULL DEFAULT 0,
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open','replayed','discarded')),
  resolved_by    uuid REFERENCES staff_users(id),
  resolved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dead_letters_open_idx ON dead_letters (created_at) WHERE status = 'open';

CREATE TABLE audit_log (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    uuid REFERENCES tenants(id),
  trace_id     uuid,
  actor_type   text NOT NULL CHECK (actor_type IN ('system','ai','staff','contact')),
  actor_id     text,
  event_type   text NOT NULL,   -- 'case.created','case.routed','message.classified','extraction.created',
                                -- 'policy.decided','action.proposed','action.executed','approval.requested',
                                -- 'approval.decided','draft.sent','case.escalated','contact.anonymized', …
  entity_type  text,
  entity_id    uuid,
  details      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_trace_idx  ON audit_log (trace_id);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id);
CREATE INDEX audit_log_tenant_idx ON audit_log (tenant_id, created_at);

CREATE OR REPLACE FUNCTION audit_log_block_changes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END $$;
CREATE TRIGGER audit_log_no_update   BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_block_changes();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_block_changes();

-- =====================================================================
-- 9. EVALUATION
-- =====================================================================
CREATE TABLE eval_cases (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pack_key             text NOT NULL,
  case_key             text NOT NULL,          -- stable id, e.g. 'inv-po-variance-001'
  channel              text NOT NULL DEFAULT 'email',
  sender_address       text,
  subject              text,
  message              text,
  form_fields          jsonb NOT NULL DEFAULT '{}'::jsonb,
  document_fixture     text,                   -- path under evals/fixtures/
  expected_intent      text NOT NULL,
  expected_priority    text,
  expected_decision    text CHECK (expected_decision IN ('auto','needs_approval','escalate','ignore')),
  expected_extraction  jsonb,                  -- required-field values to compare exactly
  expected_tools       text[] NOT NULL DEFAULT '{}',
  forbidden_tools      text[] NOT NULL DEFAULT '{}',
  tags                 text[] NOT NULL DEFAULT '{}',   -- 'must_escalate','injection','unverified','money',…
  notes                text,
  active               boolean NOT NULL DEFAULT true,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (pack_key, case_key),
  CHECK (message IS NOT NULL OR document_fixture IS NOT NULL OR form_fields <> '{}'::jsonb)
);

CREATE TABLE eval_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pack_key         text NOT NULL,
  pack_version     integer NOT NULL,
  git_sha          text,
  models           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"classify":"…","extract":"…","draft":"…"}
  prompt_versions  jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  summary          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- accuracies, gate results, cost, p50/p95
  passed_gates     boolean
);

CREATE TABLE eval_results (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_run_id         uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  eval_case_id        uuid NOT NULL REFERENCES eval_cases(id) ON DELETE CASCADE,
  actual_intent       text,
  actual_priority     text,
  actual_decision     text,
  actual_extraction   jsonb,
  actual_tools        text[],
  confidence          numeric(4,3),
  intent_correct      boolean,
  decision_correct    boolean,
  extraction_score    numeric(4,3),       -- share of required fields exactly right
  gate_violation      text,               -- 'auto_on_must_escalate','forbidden_tool_used','leak_unverified'
  cost_usd            numeric(12,6),
  latency_ms          integer,
  output              jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (eval_run_id, eval_case_id)
);

-- =====================================================================
-- 10. updated_at triggers
-- =====================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenants','tenant_settings','tenant_packs','organizations','contacts',
                           'cases','messages','knowledge_documents','drafts']
  LOOP
    EXECUTE format('CREATE TRIGGER %I_set_updated_at BEFORE UPDATE ON %I
                    FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t, t);
  END LOOP;
END $$;

-- =====================================================================
-- 11. REPORTING VIEWS (Metabase)
-- =====================================================================
CREATE VIEW v_case_outcomes AS
SELECT
  c.id AS case_id, c.tenant_id, p.key AS pack_key, p.version AS pack_version,
  c.channel, c.status, c.priority, c.created_at, c.resolved_at,
  cl.intent_key, cl.confidence, cl.method AS classification_method,
  pd.decision, pd.rule_id, pd.rule_source,
  EXTRACT(EPOCH FROM (c.resolved_at - c.created_at))::integer AS resolution_seconds,
  (SELECT count(*) FROM actions a WHERE a.case_id = c.id AND a.status = 'executed') AS actions_executed,
  (SELECT coalesce(sum(r.cost_usd),0) FROM ai_runs r WHERE r.case_id = c.id)        AS ai_cost_usd
FROM cases c
LEFT JOIN packs p ON p.id = c.pack_id
LEFT JOIN LATERAL (SELECT * FROM classifications x WHERE x.case_id = c.id ORDER BY x.created_at DESC LIMIT 1) cl ON true
LEFT JOIN LATERAL (SELECT * FROM policy_decisions d WHERE d.case_id = c.id ORDER BY d.created_at DESC LIMIT 1) pd ON true;

CREATE VIEW v_daily_kpis AS
SELECT
  tenant_id, pack_key, created_at::date AS day,
  count(*)                                              AS cases,
  count(*) FILTER (WHERE decision = 'auto')             AS automated,
  count(*) FILTER (WHERE decision = 'needs_approval')   AS needed_approval,
  count(*) FILTER (WHERE decision = 'escalate')         AS escalated,
  count(*) FILTER (WHERE decision = 'ignore')           AS ignored,
  round(100.0 * count(*) FILTER (WHERE decision = 'auto')
        / NULLIF(count(*) FILTER (WHERE decision <> 'ignore'), 0), 1) AS automation_rate_pct,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_seconds)     AS median_resolution_seconds,
  round(sum(ai_cost_usd)::numeric, 4)                                 AS ai_cost_usd
FROM v_case_outcomes
GROUP BY tenant_id, pack_key, created_at::date;

CREATE VIEW v_daily_ai_cost AS
SELECT tenant_id, created_at::date AS day, step, model,
       count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cost_usd) AS cost_usd,
       count(*) FILTER (WHERE used_fallback)        AS fallback_calls,
       count(*) FILTER (WHERE status <> 'success')  AS failed_calls
FROM ai_runs
GROUP BY 1, 2, 3, 4;

CREATE VIEW v_pending_approvals AS
SELECT ar.id AS request_id, ar.tenant_id, p.key AS pack_key, ar.case_id, ar.subject_type,
       ar.required_role, ar.reason, ar.created_at, ar.sla_due_at,
       (now() > ar.sla_due_at) AS overdue,
       c.priority, c.subject
FROM approval_requests ar
JOIN cases c ON c.id = ar.case_id
LEFT JOIN packs p ON p.id = c.pack_id
WHERE ar.status = 'pending';

-- =====================================================================
-- 12. DEMO SCHEMA — mock external systems for `mock` tool adapters
--     (replaced by Shopify / HubSpot / QuickBooks / Cal.com via `real` adapters)
-- =====================================================================
CREATE SCHEMA demo;

-- Mock shop (Shopify stand-in)
CREATE TABLE demo.shop_orders (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  order_number           text NOT NULL,
  customer_email         citext NOT NULL,
  customer_name          text NOT NULL,
  shipping_postcode      text,
  shipping_country       char(2),
  status                 text NOT NULL CHECK (status IN ('pending','paid','processing','shipped','delivered',
                                                         'cancelled','refunded','partially_refunded','payment_failed')),
  total_amount           numeric(12,2) NOT NULL,
  currency               char(3) NOT NULL,
  carrier                text,
  tracking_number        text,
  placed_at              timestamptz NOT NULL,
  shipped_at             timestamptz,
  delivered_at           timestamptz,
  estimated_delivery_at  timestamptz,
  refunded_at            timestamptz,
  scenario               text,
  UNIQUE (tenant_id, order_number)
);
CREATE TABLE demo.shop_order_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id    uuid NOT NULL REFERENCES demo.shop_orders(id) ON DELETE CASCADE,
  sku         text NOT NULL,
  name        text NOT NULL,
  quantity    integer NOT NULL CHECK (quantity > 0),
  unit_price  numeric(12,2) NOT NULL
);

-- Mock CRM (HubSpot stand-in)
CREATE TABLE demo.crm_contacts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email       citext NOT NULL,
  full_name   text,
  company     text,
  lifecycle   text NOT NULL DEFAULT 'lead' CHECK (lifecycle IN ('lead','mql','sql','customer')),
  lead_score  integer CHECK (lead_score BETWEEN 0 AND 100),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);
CREATE TABLE demo.crm_deals (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact_id  uuid NOT NULL REFERENCES demo.crm_contacts(id),
  name        text NOT NULL,
  stage       text NOT NULL DEFAULT 'new' CHECK (stage IN ('new','qualified','meeting_booked','proposal','won','lost')),
  amount      numeric(12,2),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Mock company enrichment source
CREATE TABLE demo.company_directory (
  domain          citext PRIMARY KEY,
  name            text NOT NULL,
  industry        text,
  employee_count  integer,
  country         char(2),
  store_count     integer
);

-- Mock calendar (Cal.com stand-in)
CREATE TABLE demo.calendar_slots (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  owner_email citext NOT NULL,
  starts_at   timestamptz NOT NULL,
  ends_at     timestamptz NOT NULL,
  booked_by   citext,
  CHECK (ends_at > starts_at)
);

-- Mock accounting (QuickBooks / Xero stand-in)
CREATE TABLE demo.vendors (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name            text NOT NULL,
  email_domain    citext,
  tax_id          text,
  payment_terms_days integer NOT NULL DEFAULT 30,
  bank_last4      text,
  UNIQUE (tenant_id, name)
);
CREATE TABLE demo.purchase_orders (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  vendor_id     uuid NOT NULL REFERENCES demo.vendors(id),
  po_number     text NOT NULL,
  status        text NOT NULL CHECK (status IN ('open','partially_billed','billed','closed','cancelled')),
  total_amount  numeric(12,2) NOT NULL,
  currency      char(3) NOT NULL,
  issued_at     timestamptz NOT NULL,
  UNIQUE (tenant_id, po_number)
);
CREATE TABLE demo.purchase_order_lines (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  po_id       uuid NOT NULL REFERENCES demo.purchase_orders(id) ON DELETE CASCADE,
  sku         text NOT NULL,
  description text NOT NULL,
  quantity    integer NOT NULL CHECK (quantity > 0),
  unit_price  numeric(12,2) NOT NULL
);
CREATE TABLE demo.bills (            -- invoices already posted to accounting (for duplicate checks)
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  vendor_id       uuid NOT NULL REFERENCES demo.vendors(id),
  invoice_number  text NOT NULL,
  po_id           uuid REFERENCES demo.purchase_orders(id),
  total_amount    numeric(12,2) NOT NULL,
  currency        char(3) NOT NULL,
  status          text NOT NULL CHECK (status IN ('draft','awaiting_payment','scheduled','paid','void')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, vendor_id, invoice_number)
);

COMMIT;
