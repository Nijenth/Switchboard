BEGIN;

-- Stage 1 has no execution data. Never reinterpret a legacy approval or authorization.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.actions) OR EXISTS (SELECT 1 FROM public.drafts)
     OR EXISTS (SELECT 1 FROM public.approval_requests) OR EXISTS (SELECT 1 FROM public.policy_decisions) THEN
    RAISE EXCEPTION 'execution authorization requires an explicit migration plan for existing records';
  END IF;
END $$;

ALTER TABLE public.cases ADD COLUMN policy_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE public.policy_decisions
  ADD COLUMN revision bigint NOT NULL,
  ADD COLUMN draft_id uuid,
  ADD COLUMN draft_payload_hash text,
  ADD CONSTRAINT policy_decisions_case_revision_key UNIQUE (tenant_id, case_id, revision),
  ADD CONSTRAINT policy_decisions_draft_binding CHECK ((draft_id IS NULL) = (draft_payload_hash IS NULL));

REVOKE UPDATE ON public.policy_decisions, public.cases FROM app_core;
GRANT UPDATE (tenant_pack_id, pack_id, contact_id, organization_id, channel, endpoint_id,
              external_thread_id, status, priority, queue_id, assigned_to, identity_verified,
              subject, summary, outcome, sla_due_at, last_activity_at, resolved_at)
  ON public.cases TO app_core;
GRANT INSERT (id, tenant_id, tenant_pack_id, pack_id, contact_id, organization_id, channel,
              endpoint_id, external_thread_id, status, priority, queue_id, assigned_to,
              identity_verified, subject, summary, outcome, sla_due_at, last_activity_at,
              resolved_at, created_at, updated_at) ON public.cases TO app_core;
REVOKE INSERT ON public.cases FROM app_core;

CREATE FUNCTION public.publish_policy_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF session_user <> current_user AND NEW.tenant_id IS DISTINCT FROM public.app_current_tenant() THEN
    RAISE EXCEPTION 'policy decision requires matching tenant context';
  END IF;
  -- Updating the case serializes publication with execution claims, including at higher isolation levels.
  UPDATE public.cases SET policy_revision = policy_revision + 1
   WHERE tenant_id = NEW.tenant_id AND id = NEW.case_id
   RETURNING policy_revision INTO NEW.revision;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'policy decision case not found in this tenant';
  END IF;
  IF NEW.draft_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.drafts d
     WHERE (d.tenant_id, d.case_id, d.id, d.payload_hash) =
           (NEW.tenant_id, NEW.case_id, NEW.draft_id, NEW.draft_payload_hash)
  ) THEN
    RAISE EXCEPTION 'policy decision draft binding does not match its case and payload';
  END IF;
  NEW.created_at := clock_timestamp();
  RETURN NEW;
END $$;

CREATE TRIGGER policy_decisions_publish_revision BEFORE INSERT ON public.policy_decisions
  FOR EACH ROW EXECUTE FUNCTION public.publish_policy_revision();
REVOKE ALL ON FUNCTION public.publish_policy_revision() FROM PUBLIC;

ALTER TABLE public.actions ADD COLUMN destination_account text NOT NULL;
ALTER TABLE public.actions ADD COLUMN execution_draft_id uuid REFERENCES public.drafts(id);
CREATE UNIQUE INDEX actions_one_execution_per_draft ON public.actions (execution_draft_id)
  WHERE execution_draft_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.action_payload_hash(a public.actions) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT encode(sha256(convert_to(jsonb_build_array(
    a.tenant_id, a.case_id, a.tool_key, a.risk_level, a.adapter, a.destination_account,
    a.amount, a.currency, a.input, a.idempotency_key
  )::text, 'UTF8')), 'hex')
$$;

CREATE OR REPLACE FUNCTION public.draft_payload_hash(d public.drafts) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT encode(sha256(convert_to(jsonb_build_array(
    d.tenant_id, d.case_id, d.in_reply_to_message_id, d.channel, d.recipient, d.subject,
    d.body, d.citations, d.guardrail_results
  )::text, 'UTF8')), 'hex')
$$;

CREATE FUNCTION public.draft_is_authorized(d public.drafts, current_revision bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.policy_decisions pd
    WHERE (pd.tenant_id, pd.case_id, pd.id) = (d.tenant_id, d.case_id, d.policy_decision_id)
      AND (
        (pd.revision = current_revision AND pd.decision = 'auto'
         AND pd.draft_id = d.id AND pd.draft_payload_hash = d.payload_hash)
        OR EXISTS (
          SELECT 1 FROM public.approval_requests r
          JOIN public.approval_decisions ad ON (ad.tenant_id, ad.request_id) = (r.tenant_id, r.id)
          WHERE (r.tenant_id, r.case_id, r.subject_type, r.subject_id, r.payload_hash) =
                (d.tenant_id, d.case_id, 'draft', d.id, d.payload_hash)
            AND r.status = 'approved' AND ad.decision = 'approved'
        )
      )
  )
$$;

CREATE OR REPLACE FUNCTION public.enforce_draft_rules() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  current_revision bigint;
BEGIN
  NEW.payload_hash := public.draft_payload_hash(NEW);
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'pending' THEN
      RAISE EXCEPTION 'drafts must start pending';
    END IF;
    RETURN NEW;
  END IF;

  IF (NEW.id, NEW.tenant_id, NEW.case_id) IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.case_id) THEN
    RAISE EXCEPTION 'draft identity is immutable';
  END IF;
  IF NEW.payload_hash IS DISTINCT FROM OLD.payload_hash AND (
    OLD.status <> 'pending' OR EXISTS (
      SELECT 1 FROM public.approval_requests r WHERE r.tenant_id = OLD.tenant_id
        AND r.subject_type = 'draft' AND r.subject_id = OLD.id
    )
  ) THEN
    RAISE EXCEPTION 'draft content is frozen; create a new draft';
  END IF;
  IF OLD.status <> 'pending' AND NEW.policy_decision_id IS DISTINCT FROM OLD.policy_decision_id THEN
    RAISE EXCEPTION 'draft policy binding is frozen';
  END IF;
  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;
  IF NOT ((OLD.status = 'pending' AND NEW.status IN ('approved','rejected','superseded'))
       OR (OLD.status = 'approved' AND NEW.status IN ('sent','send_failed','superseded'))
       OR (OLD.status = 'send_failed' AND NEW.status = 'superseded')) THEN
    RAISE EXCEPTION 'illegal draft transition % -> %', OLD.status, NEW.status;
  END IF;
  SELECT policy_revision INTO current_revision FROM public.cases
   WHERE tenant_id = NEW.tenant_id AND id = NEW.case_id FOR UPDATE;
  IF NEW.status = 'approved' THEN
    IF NOT public.draft_is_authorized(NEW, current_revision) THEN
      RAISE EXCEPTION 'draft requires exact policy or human authorization';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION public.enforce_action_rules() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  config jsonb;
  enrollment_enabled boolean;
  current_revision bigint;
  identity_verified boolean;
  registry_risk text;
  requires_identity boolean;
  expected_adapter text;
  expected_destination text;
  req public.approval_requests%ROWTYPE;
  draft public.drafts%ROWTYPE;
BEGIN
  IF TG_OP = 'INSERT' OR (NEW.status = 'executing' AND OLD.status <> 'executing') THEN
    SELECT c.policy_revision, c.identity_verified
      INTO current_revision, identity_verified FROM public.cases c
     WHERE (c.tenant_id, c.id) = (NEW.tenant_id, NEW.case_id) FOR UPDATE;
    SELECT tp.config, tp.enabled, t.risk_level, t.requires_verified_identity
      INTO config, enrollment_enabled, registry_risk, requires_identity
      FROM public.cases c
      JOIN public.tenant_packs tp ON (tp.tenant_id, tp.id) = (c.tenant_id, c.tenant_pack_id)
      JOIN public.pack_tools pt ON pt.pack_id = c.pack_id AND pt.tool_key = NEW.tool_key
      JOIN public.tools t ON t.key = pt.tool_key AND t.active
     WHERE (c.tenant_id, c.id) = (NEW.tenant_id, NEW.case_id);
    IF NOT FOUND THEN
      IF NOT EXISTS (SELECT 1 FROM public.tools WHERE key = NEW.tool_key AND active) THEN
        RAISE EXCEPTION 'tool % is not registered or inactive', NEW.tool_key;
      END IF;
      RAISE EXCEPTION 'tool % is not allowed for this case''s pack', NEW.tool_key;
    END IF;
    IF NEW.risk_level <> registry_risk THEN
      RAISE EXCEPTION 'risk_level % does not match registry (%) for tool %', NEW.risk_level, registry_risk, NEW.tool_key;
    END IF;
    expected_adapter := coalesce(config->'adapters'->>NEW.tool_key, 'mock');
    expected_destination := CASE WHEN expected_adapter = 'mock' THEN NEW.tenant_id::text
                                 ELSE nullif(config->'adapter_accounts'->>NEW.tool_key, '') END;
    IF NEW.adapter IS DISTINCT FROM expected_adapter OR expected_destination IS NULL THEN
      RAISE EXCEPTION 'action adapter and destination require trusted configuration';
    END IF;
    IF TG_OP = 'INSERT' THEN
      IF NEW.status NOT IN ('proposed','pending_approval') THEN
        RAISE EXCEPTION 'actions must be created as proposed or pending_approval, not %', NEW.status;
      END IF;
      NEW.destination_account := expected_destination;
      NEW.payload_hash := public.action_payload_hash(NEW);
      NEW.execution_draft_id := NULL;
      NEW.claimed_by := NULL;
      NEW.lease_expires_at := NULL;
      NEW.approved_by := NULL;
      NEW.attempts := 0;
      RETURN NEW;
    END IF;
    IF NEW.destination_account IS DISTINCT FROM expected_destination OR NOT enrollment_enabled THEN
      RAISE EXCEPTION 'action destination or pack enrollment is no longer authorized';
    END IF;
    IF requires_identity AND NOT identity_verified THEN
      RAISE EXCEPTION 'action requires verified identity';
    END IF;
  END IF;

  IF OLD.status IN ('executed','rejected','cancelled') THEN
    RAISE EXCEPTION 'action % is final (%) and cannot change', OLD.id, OLD.status;
  END IF;
  IF (NEW.id, NEW.tenant_id, NEW.case_id, NEW.tool_key, NEW.risk_level, NEW.adapter,
      NEW.destination_account, NEW.idempotency_key, NEW.policy_decision_id) IS DISTINCT FROM
     (OLD.id, OLD.tenant_id, OLD.case_id, OLD.tool_key, OLD.risk_level, OLD.adapter,
      OLD.destination_account, OLD.idempotency_key, OLD.policy_decision_id) THEN
    RAISE EXCEPTION 'action identity, destination and policy binding are immutable';
  END IF;
  NEW.payload_hash := public.action_payload_hash(NEW);
  NEW.execution_draft_id := OLD.execution_draft_id;
  SELECT * INTO req FROM public.approval_requests r
   WHERE (r.tenant_id, r.case_id, r.subject_type, r.subject_id) =
         (NEW.tenant_id, NEW.case_id, 'action', NEW.id)
   ORDER BY r.created_at DESC, r.id DESC LIMIT 1;
  IF NEW.payload_hash IS DISTINCT FROM OLD.payload_hash AND (OLD.status <> 'proposed' OR req.id IS NOT NULL) THEN
    RAISE EXCEPTION 'action payload is frozen after approval is requested';
  END IF;
  IF NEW.status = OLD.status THEN
    IF (NEW.claimed_by, NEW.lease_expires_at, NEW.attempts, NEW.approved_by) IS DISTINCT FROM
       (OLD.claimed_by, OLD.lease_expires_at, OLD.attempts, OLD.approved_by) THEN
      RAISE EXCEPTION 'action authority cannot change without a transition';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT ((OLD.status = 'proposed' AND NEW.status IN ('pending_approval','executing','rejected','cancelled'))
       OR (OLD.status = 'pending_approval' AND NEW.status IN ('approved','rejected','cancelled'))
       OR (OLD.status = 'approved' AND NEW.status IN ('executing','cancelled'))
       OR (OLD.status = 'executing' AND NEW.status IN ('executed','failed','unknown_outcome'))
       OR (OLD.status = 'unknown_outcome' AND NEW.status IN ('executed','failed'))
       OR (OLD.status = 'failed' AND NEW.status = 'cancelled')) THEN
    RAISE EXCEPTION 'illegal action transition % -> %', OLD.status, NEW.status;
  END IF;

  IF NEW.status = 'approved' OR (NEW.status = 'executing' AND
     (OLD.status = 'approved' OR NEW.risk_level = 'money' OR req.id IS NOT NULL)) THEN
    IF req.id IS NULL THEN
      RAISE EXCEPTION 'action % has no approval request', NEW.id;
    END IF;
    IF req.status <> 'approved' OR req.payload_hash IS DISTINCT FROM NEW.payload_hash THEN
      RAISE EXCEPTION 'action requires an approved request for its exact payload';
    END IF;
    IF NEW.status = 'executing' AND OLD.status <> 'approved' THEN
      RAISE EXCEPTION 'action must transition through approval before executing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.approval_decisions ad
                    WHERE (ad.tenant_id, ad.request_id, ad.reviewer_id) =
                          (NEW.tenant_id, req.id, NEW.approved_by) AND ad.decision = 'approved') THEN
      RAISE EXCEPTION 'approved_by must be the reviewer who approved request %', req.id;
    END IF;
  ELSIF NEW.status = 'executing' AND NEW.risk_level <> 'read' THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.policy_decisions pd
       WHERE (pd.tenant_id, pd.case_id, pd.id, pd.revision) =
             (NEW.tenant_id, NEW.case_id, NEW.policy_decision_id, current_revision)
         AND pd.decision = 'auto' AND NEW.tool_key = ANY(pd.allowed_tools)
    ) THEN
      RAISE EXCEPTION 'automatic execution requires a current policy allow for the tool';
    END IF;
  END IF;

  IF NEW.status = 'executing' THEN
    NEW.attempts := OLD.attempts + 1;
    IF NEW.claimed_by IS NULL OR NEW.lease_expires_at IS NULL OR NEW.lease_expires_at <= clock_timestamp() THEN
      RAISE EXCEPTION 'executing requires claimed_by and a future lease_expires_at';
    END IF;
    IF NEW.risk_level = 'external_comm' THEN
      SELECT * INTO draft FROM public.drafts d
       WHERE (d.tenant_id, d.case_id, d.id, d.policy_decision_id) =
             (NEW.tenant_id, NEW.case_id, (NEW.input->>'draft_id')::uuid, NEW.policy_decision_id);
      IF NOT FOUND OR draft.status <> 'approved' OR NOT public.draft_is_authorized(draft, current_revision) THEN
        RAISE EXCEPTION 'external communication requires input.draft_id of an approved draft with current authority';
      END IF;
      NEW.execution_draft_id := draft.id;
    END IF;
  END IF;
  RETURN NEW;
END $$;

COMMIT;
