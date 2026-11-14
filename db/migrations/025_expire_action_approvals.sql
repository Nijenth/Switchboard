BEGIN;

CREATE OR REPLACE FUNCTION public.expire_approval_request(p_request_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  req public.approval_requests%ROWTYPE;
  action_row public.actions%ROWTYPE;
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
  IF req.subject_type = 'action' THEN
    UPDATE public.actions SET status = 'cancelled'
     WHERE (tenant_id, case_id, id, payload_hash) =
           (req.tenant_id, req.case_id, req.subject_id, req.payload_hash)
       AND status IN ('proposed', 'pending_approval')
     RETURNING * INTO action_row;
    IF NOT FOUND THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'approval action is no longer pending';
    END IF;
    INSERT INTO public.audit_log (
      tenant_id, trace_id, actor_type, event_type, entity_type, entity_id, details
    ) VALUES (
      tenant, action_row.trace_id, 'system', 'action.cancelled', 'action', action_row.id,
      jsonb_build_object('reason', 'approval_expired', 'approval_request_id', req.id)
    );
  END IF;
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION public.expire_approval_request(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.expire_approval_request(uuid) TO app_core;

COMMIT;
