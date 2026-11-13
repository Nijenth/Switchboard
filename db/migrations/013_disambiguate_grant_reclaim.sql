BEGIN;

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
COMMIT;
