BEGIN;

ALTER TABLE public.approval_decisions ADD CONSTRAINT approval_decisions_one_per_request UNIQUE (request_id);
DROP INDEX public.approval_decisions_request_idx;

REVOKE UPDATE ON public.approval_requests FROM app_core;
GRANT UPDATE (slack_channel, slack_message_ts) ON public.approval_requests TO app_core;
REVOKE INSERT, UPDATE ON public.approval_decisions FROM app_core;

CREATE OR REPLACE FUNCTION public.capture_approval_payload() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  subject_hash text;
  trusted_role text;
  sla_minutes integer;
BEGIN
  IF NEW.subject_type = 'action' THEN
    SELECT a.payload_hash,
           CASE WHEN a.risk_level = 'money' THEN coalesce(pt.approval_role, 'manager')
                ELSE coalesce(pt.approval_role, pd.required_role, 'manager') END
      INTO subject_hash, trusted_role
      FROM public.actions a
      JOIN public.cases c ON (c.tenant_id, c.id) = (a.tenant_id, a.case_id)
      JOIN public.pack_tools pt ON (pt.pack_id, pt.tool_key) = (c.pack_id, a.tool_key)
      LEFT JOIN public.policy_decisions pd ON (pd.tenant_id, pd.case_id, pd.id) =
                                             (a.tenant_id, a.case_id, a.policy_decision_id)
     WHERE (a.tenant_id, a.case_id, a.id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id);
  ELSIF NEW.subject_type = 'draft' THEN
    SELECT d.payload_hash, coalesce(pt.approval_role, pd.required_role, 'manager')
      INTO subject_hash, trusted_role
      FROM public.drafts d
      JOIN public.cases c ON (c.tenant_id, c.id) = (d.tenant_id, d.case_id)
      LEFT JOIN public.pack_tools pt ON pt.pack_id = c.pack_id AND pt.tool_key = 'send_reply'
      LEFT JOIN public.policy_decisions pd ON (pd.tenant_id, pd.case_id, pd.id) =
                                             (d.tenant_id, d.case_id, d.policy_decision_id)
     WHERE (d.tenant_id, d.case_id, d.id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id);
  ELSE
    SELECT encode(sha256(convert_to(e.data::text, 'UTF8')), 'hex'), 'manager'
      INTO subject_hash, trusted_role FROM public.extractions e
     WHERE (e.tenant_id, e.case_id, e.id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id);
  END IF;
  IF subject_hash IS NULL THEN
    RAISE EXCEPTION 'approval subject % % not found in this tenant/case', NEW.subject_type, NEW.subject_id;
  END IF;
  SELECT approval_sla_minutes INTO sla_minutes FROM public.tenant_settings WHERE tenant_id = NEW.tenant_id;
  IF sla_minutes IS NULL THEN
    RAISE EXCEPTION 'approval SLA is missing for this tenant';
  END IF;
  NEW.payload_hash := subject_hash;
  NEW.required_role := trusted_role;
  NEW.status := 'pending';
  NEW.resolved_at := NULL;
  NEW.created_at := clock_timestamp();
  NEW.sla_due_at := NEW.created_at + make_interval(mins => sla_minutes);
  RETURN NEW;
END $$;

DROP TRIGGER approval_decisions_apply ON public.approval_decisions;
DROP FUNCTION public.apply_approval_decision();
DROP TRIGGER approval_decisions_check_reviewer ON public.approval_decisions;
DROP FUNCTION public.check_approval_reviewer();

-- Core supplies the reviewer resolved from a verified interaction. This function checks
-- the reviewer against the locked request and never accepts a caller-supplied timestamp.
CREATE FUNCTION public.record_approval_decision(
  p_request_id uuid, p_reviewer_id uuid, p_decision text, p_channel text,
  p_edited_payload jsonb DEFAULT NULL, p_comment text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  req public.approval_requests%ROWTYPE;
  reviewer public.staff_users%ROWTYPE;
  decided timestamptz;
  decision_id uuid;
  subject_hash text;
BEGIN
  SELECT * INTO req FROM public.approval_requests
    WHERE tenant_id = tenant AND id = p_request_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'approval request not found in this tenant';
  END IF;
  IF req.status <> 'pending' THEN
    RAISE EXCEPTION 'approval request is already %', req.status;
  END IF;
  decided := clock_timestamp();
  IF decided >= req.sla_due_at THEN
    RAISE EXCEPTION 'approval request is past its SLA';
  END IF;

  SELECT * INTO reviewer FROM public.staff_users WHERE tenant_id = tenant AND id = p_reviewer_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'reviewer not found in this tenant';
  END IF;
  IF NOT reviewer.active THEN
    RAISE EXCEPTION 'reviewer is inactive';
  END IF;
  IF NOT (reviewer.role IN ('admin', req.required_role)
          OR (req.required_role = 'agent' AND reviewer.role IN ('manager','finance'))) THEN
    RAISE EXCEPTION 'role % cannot satisfy required role %', reviewer.role, req.required_role;
  END IF;
  CASE req.subject_type
    WHEN 'action' THEN
      SELECT payload_hash INTO subject_hash FROM public.actions
       WHERE (tenant_id, case_id, id) = (tenant, req.case_id, req.subject_id);
    WHEN 'draft' THEN
      SELECT payload_hash INTO subject_hash FROM public.drafts
       WHERE (tenant_id, case_id, id) = (tenant, req.case_id, req.subject_id);
    ELSE
      SELECT encode(sha256(convert_to(data::text, 'UTF8')), 'hex') INTO subject_hash
        FROM public.extractions WHERE (tenant_id, case_id, id) = (tenant, req.case_id, req.subject_id);
  END CASE;
  IF subject_hash IS NULL OR subject_hash IS DISTINCT FROM req.payload_hash THEN
    RAISE EXCEPTION '% changed after approval was requested', req.subject_type;
  END IF;

  INSERT INTO public.approval_decisions (tenant_id, request_id, reviewer_id, decision, channel,
                                         edited_payload, comment, decided_at)
    VALUES (tenant, req.id, reviewer.id, p_decision, p_channel, p_edited_payload, p_comment, decided)
    RETURNING id INTO decision_id;
  UPDATE public.approval_requests SET status = p_decision, resolved_at = decided
   WHERE tenant_id = tenant AND id = req.id;
  INSERT INTO public.audit_log (tenant_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    VALUES (tenant, 'staff', reviewer.id::text, 'approval.decided', 'approval_request', req.id,
            jsonb_build_object('decision_id', decision_id, 'decision', p_decision));
  RETURN decision_id;
END $$;

CREATE FUNCTION public.expire_approval_request(p_request_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  req public.approval_requests%ROWTYPE;
  expired timestamptz;
BEGIN
  SELECT * INTO req FROM public.approval_requests
    WHERE tenant_id = tenant AND id = p_request_id FOR UPDATE;
  IF NOT FOUND OR req.status <> 'pending' THEN
    RETURN false;
  END IF;
  expired := clock_timestamp();
  IF expired < req.sla_due_at THEN
    RETURN false;
  END IF;
  UPDATE public.approval_requests SET status = 'expired', resolved_at = expired
   WHERE tenant_id = tenant AND id = req.id;
  INSERT INTO public.audit_log (tenant_id, actor_type, event_type, entity_type, entity_id)
    VALUES (tenant, 'system', 'approval.expired', 'approval_request', req.id);
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION public.record_approval_decision(uuid, uuid, text, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.expire_approval_request(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_approval_decision(uuid, uuid, text, text, jsonb, text),
                          public.expire_approval_request(uuid) TO app_core;

COMMIT;
