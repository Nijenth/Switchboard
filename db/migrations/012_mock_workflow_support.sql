BEGIN;

CREATE TABLE demo.outbound_receipts (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id),
  action_id uuid NOT NULL,
  idempotency_key text NOT NULL,
  external_ref uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, action_id),
  UNIQUE (tenant_id, idempotency_key)
);
GRANT SELECT, INSERT ON demo.outbound_receipts TO app_n8n_mock;

CREATE FUNCTION public.guard_reconciliation_absence() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.outcome = 'absent' AND NOT EXISTS (
    SELECT 1 FROM public.actions a WHERE (a.tenant_id, a.id, a.attempts) =
      (NEW.tenant_id, NEW.action_id, NEW.attempt)
      AND a.lease_expires_at + interval '30 seconds' <= clock_timestamp()
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514',
      MESSAGE = 'absent evidence requires a 30 second grace period after lease expiry';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reconciliation_absence_grace BEFORE INSERT ON public.reconciliation_evidence
  FOR EACH ROW EXECUTE FUNCTION public.guard_reconciliation_absence();

CREATE FUNCTION public.apply_slack_approval_to_subject() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  ad public.approval_decisions%ROWTYPE;
BEGIN
  SELECT * INTO ad FROM public.approval_decisions
   WHERE (tenant_id,request_id,channel,decision) = (NEW.tenant_id,NEW.id,'slack','approved');
  IF NOT FOUND THEN RETURN NEW; END IF;
  IF NEW.subject_type = 'action' THEN
    UPDATE public.actions SET status='approved', approved_by=ad.reviewer_id
     WHERE (tenant_id,case_id,id) = (NEW.tenant_id,NEW.case_id,NEW.subject_id)
       AND status='pending_approval';
  ELSIF NEW.subject_type = 'draft' THEN
    UPDATE public.drafts SET status='approved'
     WHERE (tenant_id,case_id,id) = (NEW.tenant_id,NEW.case_id,NEW.subject_id)
       AND status='pending';
  ELSE
    RETURN NEW;
  END IF;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Slack approval subject is no longer pending';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approval_requests_apply_slack_approval AFTER UPDATE OF status ON public.approval_requests
  FOR EACH ROW WHEN (OLD.status='pending' AND NEW.status='approved')
  EXECUTE FUNCTION public.apply_slack_approval_to_subject();
REVOKE ALL ON FUNCTION public.apply_slack_approval_to_subject() FROM PUBLIC;

CREATE FUNCTION public.replace_unused_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  g public.execution_grants%ROWTYPE;
  revision bigint;
  r public.approval_requests%ROWTYPE;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' OR a.adapter <> 'mock' OR a.claimed_by <> p_worker THEN
    RETURN NULL;
  END IF;
  SELECT * INTO g FROM public.execution_grants
   WHERE (tenant_id,action_id,attempt,worker) = (tenant,a.id,a.attempts,p_worker) FOR UPDATE;
  IF NOT FOUND OR g.consumed_at IS NOT NULL OR g.expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  SELECT policy_revision INTO revision FROM public.cases WHERE (tenant_id,id) = (tenant,a.case_id) FOR UPDATE;
  IF revision = g.policy_revision THEN RETURN NULL; END IF;
  IF a.approved_by IS NOT NULL THEN
    SELECT * INTO r FROM public.approval_requests
     WHERE (tenant_id,case_id,subject_type,subject_id) = (tenant,a.case_id,'action',a.id)
     ORDER BY created_at DESC,id DESC LIMIT 1;
    IF NOT FOUND OR r.status <> 'approved' OR r.payload_hash IS DISTINCT FROM a.payload_hash OR
       NOT public.approval_policy_is_current(r,revision) OR NOT EXISTS (
         SELECT 1 FROM public.approval_decisions ad WHERE
           (ad.tenant_id,ad.request_id,ad.reviewer_id,ad.decision) = (tenant,r.id,a.approved_by,'approved')
       ) THEN RETURN NULL; END IF;
  ELSIF a.risk_level <> 'read' AND NOT EXISTS (
    SELECT 1 FROM public.policy_decisions p WHERE
      (p.tenant_id,p.case_id,p.id,p.revision,p.decision) =
      (tenant,a.case_id,a.policy_decision_id,revision,'auto') AND a.tool_key = ANY(p.allowed_tools)
  ) THEN RETURN NULL;
  END IF;
  UPDATE public.execution_grants SET token_digest = p_digest, policy_revision = revision
   WHERE token_digest = g.token_digest;
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id)
    VALUES (tenant,a.trace_id,'system','action.grant_replaced','action',a.id);
  RETURN g.expires_at;
END $$;
REVOKE ALL ON FUNCTION public.replace_unused_mock_grant(uuid,text,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.replace_unused_mock_grant(uuid,text,bytea) TO app_core;

COMMIT;
