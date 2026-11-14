BEGIN;

-- Only complete_mock_read (SECURITY DEFINER) may change cases.identity_verified (ADR 0021).
REVOKE INSERT (identity_verified), UPDATE (identity_verified) ON public.cases FROM app_core;

CREATE FUNCTION public.order_identity_is_verified(
  p_tenant uuid, p_case uuid, p_message uuid, p_order text
) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT p_message IS NOT NULL
     AND nullif(p_order, '') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM public.cases c
        WHERE (c.tenant_id, c.id) = (p_tenant, p_case) AND c.identity_verified
     )
     AND EXISTS (
       SELECT 1 FROM public.actions ic
       JOIN public.tools t ON t.key = ic.tool_key AND t.active
        WHERE (ic.tenant_id, ic.case_id, ic.message_id) = (p_tenant, p_case, p_message)
          AND ic.risk_level = 'read' AND ic.status = 'executed'
          AND coalesce(t.output_schema -> 'properties' ? 'identity_verified', false)
          AND ic.input ->> 'order_number' = p_order
          AND ic.output -> 'order_found' = 'true'::jsonb
          AND ic.output -> 'identity_verified' = 'true'::jsonb
          AND NOT EXISTS (
            SELECT 1 FROM public.actions bad
            JOIN public.tools bt ON bt.key = bad.tool_key
             WHERE (bad.tenant_id, bad.case_id) = (p_tenant, p_case)
               AND bad.risk_level = 'read' AND bad.status = 'executed'
               AND coalesce(bt.output_schema -> 'properties' ? 'identity_verified', false)
               AND (bad.output -> 'identity_verified' IS DISTINCT FROM 'true'::jsonb
                    OR bad.output -> 'order_found' IS DISTINCT FROM 'true'::jsonb)
               AND (bad.message_id = p_message OR bad.executed_at >= ic.executed_at)
          )
     )
$$;

CREATE FUNCTION public.order_identity_is_verified(a public.actions) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT public.order_identity_is_verified(
    a.tenant_id, a.case_id, a.message_id, a.input ->> 'order_number'
  )
$$;

REVOKE ALL ON FUNCTION public.order_identity_is_verified(uuid, uuid, uuid, text),
  public.order_identity_is_verified(public.actions) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.order_identity_is_verified(uuid, uuid, uuid, text),
  public.order_identity_is_verified(public.actions) TO app_core;

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
  reclaiming_read boolean := false;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    reclaiming_read := OLD.status = 'executing' AND NEW.status = 'executing'
      AND OLD.risk_level = 'read' AND OLD.lease_expires_at IS NOT NULL
      AND OLD.lease_expires_at <= clock_timestamp()
      AND (NEW.claimed_by, NEW.lease_expires_at) IS DISTINCT FROM (OLD.claimed_by, OLD.lease_expires_at);
  END IF;

  IF TG_OP = 'INSERT' OR (NEW.status = 'executing' AND (OLD.status <> 'executing' OR reclaiming_read)) THEN
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
    IF NEW.tool_key = 'order_lookup' AND NOT public.order_identity_is_verified(NEW) THEN
      RAISE EXCEPTION 'order_lookup requires verified identity for the same message and order';
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
  IF (NEW.id, NEW.tenant_id, NEW.case_id, NEW.message_id, NEW.tool_key, NEW.risk_level, NEW.adapter,
      NEW.destination_account, NEW.idempotency_key, NEW.policy_decision_id) IS DISTINCT FROM
     (OLD.id, OLD.tenant_id, OLD.case_id, OLD.message_id, OLD.tool_key, OLD.risk_level, OLD.adapter,
      OLD.destination_account, OLD.idempotency_key, OLD.policy_decision_id) THEN
    RAISE EXCEPTION 'action identity, destination and policy binding are immutable';
  END IF;
  IF NEW.tool_key = 'order_lookup' AND NEW.input IS DISTINCT FROM OLD.input
     AND NOT public.order_identity_is_verified(NEW) THEN
    RAISE EXCEPTION 'order_lookup requires verified identity for the same message and order';
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
  IF NEW.status = OLD.status AND NOT reclaiming_read THEN
    IF (NEW.claimed_by, NEW.lease_expires_at, NEW.attempts, NEW.approved_by) IS DISTINCT FROM
       (OLD.claimed_by, OLD.lease_expires_at, OLD.attempts, OLD.approved_by) THEN
      RAISE EXCEPTION 'action authority cannot change without a transition';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT (reclaiming_read
       OR (OLD.status = 'proposed' AND NEW.status IN ('pending_approval','executing','rejected','cancelled'))
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

CREATE OR REPLACE FUNCTION public.claim_mock_action(p_action uuid, p_worker text, p_digest bytea)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  claimed public.actions%ROWTYPE;
BEGIN
  IF p_worker IS NULL OR length(p_worker) NOT BETWEEN 1 AND 128 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid execution worker';
  END IF;
  UPDATE public.actions a SET status = 'executing', claimed_by = p_worker,
      lease_expires_at = clock_timestamp() + interval '45 seconds'
    WHERE (a.tenant_id, a.id) = (tenant, p_action) AND a.adapter = 'mock'
      AND (a.status IN ('proposed','approved')
           OR (a.risk_level = 'read' AND a.status = 'executing'
               AND a.lease_expires_at <= clock_timestamp()))
    RETURNING * INTO claimed;
  IF NOT FOUND THEN RETURN NULL; END IF;
  INSERT INTO public.execution_grants (token_digest, tenant_id, action_id, attempt, worker,
    payload_hash, destination_account, policy_revision, expires_at)
  SELECT p_digest, tenant, claimed.id, claimed.attempts, p_worker, claimed.payload_hash,
    claimed.destination_account, c.policy_revision, claimed.lease_expires_at
  FROM public.cases c WHERE (c.tenant_id, c.id) = (tenant, claimed.case_id);
  INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id)
  VALUES (tenant, claimed.trace_id, 'system', 'action.claimed', 'action', claimed.id);
  RETURN claimed.lease_expires_at;
END $$;

CREATE OR REPLACE FUNCTION public.consume_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  revision bigint;
  adapter_allowed boolean;
  g public.execution_grants%ROWTYPE;
  dispatch jsonb;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' THEN RETURN NULL; END IF;
  SELECT c.policy_revision,
         tp.enabled AND tn.status = 'active' AND t.active AND t.risk_level = a.risk_level
         AND (NOT t.requires_verified_identity OR c.identity_verified)
         AND (a.tool_key <> 'order_lookup' OR public.order_identity_is_verified(a))
         AND coalesce(tp.config->'adapters'->>a.tool_key, 'mock') = 'mock'
         AND a.destination_account = a.tenant_id::text
    INTO revision, adapter_allowed
    FROM public.cases c
    JOIN public.tenants tn ON tn.id = c.tenant_id
    JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id) = (c.tenant_id,c.tenant_pack_id)
    JOIN public.pack_tools pt ON (pt.pack_id, pt.tool_key) = (c.pack_id, a.tool_key)
    JOIN public.tools t ON t.key = pt.tool_key
   WHERE (c.tenant_id,c.id) = (tenant,a.case_id)
   FOR UPDATE OF c FOR SHARE OF tn, tp, pt, t;
  IF NOT FOUND OR NOT adapter_allowed OR a.adapter <> 'mock' THEN RETURN NULL; END IF;
  UPDATE public.execution_grants SET consumed_at = clock_timestamp()
   WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
     AND consumed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account
     AND policy_revision = revision
   RETURNING * INTO g;
  IF NOT FOUND THEN RETURN NULL; END IF;
  dispatch := jsonb_build_object('tool_key', a.tool_key, 'adapter', a.adapter, 'destination', a.destination_account,
     'payload_hash', a.payload_hash, 'input', a.input, 'amount', a.amount, 'currency', a.currency,
     'idempotency_key', a.idempotency_key, 'expires_at', g.expires_at, 'trace_id', a.trace_id);
  IF a.execution_draft_id IS NOT NULL THEN
    SELECT dispatch || jsonb_build_object('draft', jsonb_build_object(
      'id', d.id, 'channel', d.channel, 'recipient', d.recipient, 'subject', d.subject, 'body', d.body))
      INTO dispatch FROM public.drafts d
     WHERE (d.tenant_id,d.case_id,d.id) = (tenant,a.case_id,a.execution_draft_id)
       AND d.status = 'approved' AND public.draft_is_authorized(d,revision);
    IF NOT FOUND THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'execution draft authority changed';
    END IF;
  END IF;
  RETURN dispatch;
END $$;

CREATE OR REPLACE FUNCTION public.replace_unused_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  g public.execution_grants%ROWTYPE;
  case_revision bigint;
  r public.approval_requests%ROWTYPE;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' OR a.adapter <> 'mock' OR a.claimed_by <> p_worker THEN
    RETURN NULL;
  END IF;
  IF a.tool_key = 'order_lookup' AND NOT public.order_identity_is_verified(a) THEN
    RETURN NULL;
  END IF;
  SELECT * INTO g FROM public.execution_grants
   WHERE (tenant_id,action_id,attempt,worker) = (tenant,a.id,a.attempts,p_worker) FOR UPDATE;
  IF NOT FOUND OR g.consumed_at IS NOT NULL OR g.expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  SELECT policy_revision INTO case_revision FROM public.cases WHERE (tenant_id,id) = (tenant,a.case_id) FOR UPDATE;
  IF case_revision = g.policy_revision THEN RETURN NULL; END IF;
  IF a.approved_by IS NOT NULL THEN
    SELECT * INTO r FROM public.approval_requests
     WHERE (tenant_id,case_id,subject_type,subject_id) = (tenant,a.case_id,'action',a.id)
     ORDER BY created_at DESC,id DESC LIMIT 1;
    IF NOT FOUND OR r.status <> 'approved' OR r.payload_hash IS DISTINCT FROM a.payload_hash OR
       NOT public.approval_policy_is_current(r,case_revision) OR NOT EXISTS (
         SELECT 1 FROM public.approval_decisions ad WHERE
           (ad.tenant_id,ad.request_id,ad.reviewer_id,ad.decision) = (tenant,r.id,a.approved_by,'approved')
       ) THEN RETURN NULL; END IF;
  ELSIF a.risk_level <> 'read' AND NOT EXISTS (
    SELECT 1 FROM public.policy_decisions p WHERE
      (p.tenant_id,p.case_id,p.id,p.revision,p.decision) =
      (tenant,a.case_id,a.policy_decision_id,case_revision,'auto') AND a.tool_key = ANY(p.allowed_tools)
  ) THEN RETURN NULL;
  END IF;
  UPDATE public.execution_grants SET token_digest = p_digest, policy_revision = case_revision
   WHERE token_digest = g.token_digest;
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id)
    VALUES (tenant,a.trace_id,'system','action.grant_replaced','action',a.id);
  RETURN g.expires_at;
END $$;

CREATE OR REPLACE FUNCTION public.complete_mock_action(p_action uuid, p_worker text, p_digest bytea, p_ref text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
BEGIN
  IF p_ref IS NULL OR length(p_ref) NOT BETWEEN 1 AND 512 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'execution requires an external reference';
  END IF;
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF a.risk_level = 'read' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'read actions require an output payload';
  END IF;
  IF a.status = 'executed' AND a.external_ref = p_ref THEN
    RETURN EXISTS (SELECT 1 FROM public.execution_grants
       WHERE (tenant_id,action_id,attempt,worker,token_digest) = (tenant,a.id,a.attempts,p_worker,p_digest)
         AND completed_at IS NOT NULL);
  END IF;
  IF a.status <> 'executing' THEN RETURN false; END IF;
  UPDATE public.execution_grants SET completed_at = clock_timestamp()
   WHERE (tenant_id,action_id,attempt,worker,token_digest) = (tenant,a.id,a.attempts,p_worker,p_digest)
     AND consumed_at IS NOT NULL AND completed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE public.actions SET status = 'executed', external_ref = p_ref, executed_at = clock_timestamp()
   WHERE (tenant_id,id) = (tenant,a.id);
  IF a.execution_draft_id IS NOT NULL THEN
    UPDATE public.drafts SET status = 'sent' WHERE (tenant_id,id) = (tenant,a.execution_draft_id)
      AND status = 'approved';
  END IF;
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id)
    VALUES (tenant,a.trace_id,'system','action.executed','action',a.id);
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION public.complete_mock_read(
  p_action uuid, p_worker text, p_digest bytea, p_ref text, p_output jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  declares_identity boolean;
BEGIN
  IF p_ref IS NULL OR length(p_ref) NOT BETWEEN 1 AND 512 OR p_output IS NULL
     OR jsonb_typeof(p_output) <> 'object' OR octet_length(p_output::text) > 16384 THEN
    RAISE EXCEPTION USING ERRCODE = '22023',
      MESSAGE = 'read completion requires a reference and an object output';
  END IF;
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.risk_level <> 'read' OR a.adapter <> 'mock' THEN
    RETURN false;
  END IF;
  SELECT coalesce(t.output_schema -> 'properties' ? 'identity_verified', false)
    INTO declares_identity FROM public.tools t WHERE t.key = a.tool_key;
  IF declares_identity AND p_output -> 'identity_verified' = 'true'::jsonb
     AND p_output -> 'order_found' IS DISTINCT FROM 'true'::jsonb THEN
    RAISE EXCEPTION USING ERRCODE = '22023',
      MESSAGE = 'verified identity requires a found order';
  END IF;
  IF a.status = 'executed' AND a.external_ref = p_ref AND a.output = p_output THEN
    RETURN EXISTS (SELECT 1 FROM public.execution_grants
       WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
         AND completed_at IS NOT NULL);
  END IF;
  IF a.status <> 'executing' THEN
    RETURN false;
  END IF;
  UPDATE public.execution_grants SET completed_at = clock_timestamp()
   WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
     AND consumed_at IS NOT NULL AND completed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE public.actions
     SET status = 'executed', external_ref = p_ref, output = p_output, executed_at = clock_timestamp()
   WHERE (tenant_id, id) = (tenant, a.id);
  INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id)
    VALUES (tenant, a.trace_id, 'system', 'action.executed', 'action', a.id);

  IF declares_identity THEN
    IF p_output -> 'identity_verified' = 'true'::jsonb THEN
      UPDATE public.cases SET identity_verified = true
       WHERE (tenant_id, id) = (tenant, a.case_id) AND NOT identity_verified;
      IF FOUND THEN
        INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id, details)
          VALUES (tenant, a.trace_id, 'system', 'case.identity_verified', 'case', a.case_id,
                  jsonb_build_object('action_id', a.id, 'tool_key', a.tool_key));
      END IF;
    ELSE
      UPDATE public.cases SET identity_verified = false
       WHERE (tenant_id, id) = (tenant, a.case_id) AND identity_verified;
    END IF;
  END IF;
  RETURN true;
END $$;

COMMIT;
