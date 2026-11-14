BEGIN;

CREATE FUNCTION public.has_current_mock_reply_review(a public.actions, current_revision bigint)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT a.tool_key = 'send_reply' AND a.adapter = 'mock' AND a.risk_level = 'external_comm'
    AND a.amount IS NULL AND a.currency IS NULL AND a.approved_by IS NULL
    AND EXISTS (
      SELECT 1 FROM public.drafts d
      JOIN public.policy_decisions p ON (p.tenant_id,p.case_id,p.id) =
        (d.tenant_id,d.case_id,d.policy_decision_id)
      WHERE (d.tenant_id,d.case_id,d.in_reply_to_message_id,d.policy_decision_id) =
            (a.tenant_id,a.case_id,a.message_id,a.policy_decision_id)
        AND a.input = jsonb_build_object('draft_id',d.id)
        AND a.idempotency_key = 'draft-send:' || d.id::text
        AND d.status = 'approved' AND p.decision = 'needs_approval'
        AND p.revision = current_revision AND p.draft_id = d.id AND p.draft_payload_hash = d.payload_hash
        AND public.draft_is_authorized(d,current_revision)
    )
$$;
REVOKE ALL ON FUNCTION public.has_current_mock_reply_review(public.actions,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.has_current_mock_reply_review(public.actions,bigint) TO app_core;

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
    IF NEW.status = 'approved' THEN
      SELECT policy_revision INTO current_revision FROM public.cases
       WHERE tenant_id = NEW.tenant_id AND id = NEW.case_id FOR UPDATE;
    END IF;
    IF NOT public.approval_policy_is_current(req, current_revision) THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'a stricter policy decision requires fresh approval';
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
    IF NOT public.has_current_mock_reply_review(NEW, current_revision) AND NOT EXISTS (
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
