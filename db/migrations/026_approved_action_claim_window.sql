BEGIN;

ALTER TABLE public.tenant_settings
  ADD COLUMN approval_claim_window_minutes integer NOT NULL DEFAULT 60
    CHECK (approval_claim_window_minutes > 0);

CREATE INDEX approval_requests_action_subject_idx ON public.approval_requests
  (tenant_id, case_id, subject_id, created_at DESC, id DESC)
  WHERE subject_type = 'action';

CREATE FUNCTION public.cancel_unclaimed_approved_actions() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH due AS (
    SELECT a.tenant_id, a.id, r.id AS request_id
      FROM public.actions a
      JOIN public.tenant_settings s ON s.tenant_id = a.tenant_id
      CROSS JOIN LATERAL (
        SELECT r.id, r.status, r.payload_hash, r.resolved_at
          FROM public.approval_requests r
         WHERE (r.tenant_id,r.case_id,r.subject_type,r.subject_id) =
               (a.tenant_id,a.case_id,'action',a.id)
         ORDER BY r.created_at DESC, r.id DESC LIMIT 1
      ) r
     WHERE a.tenant_id = public.app_current_tenant()
       AND a.status IN ('pending_approval','approved') AND a.attempts = 0
       AND r.status = 'approved' AND r.payload_hash = a.payload_hash
       AND r.resolved_at + make_interval(mins => s.approval_claim_window_minutes) <= statement_timestamp()
     ORDER BY r.resolved_at, a.id LIMIT 100
     FOR UPDATE OF a SKIP LOCKED
  ), cancelled AS (
    UPDATE public.actions a SET status = 'cancelled'
      FROM due
     WHERE (a.tenant_id,a.id) = (due.tenant_id,due.id)
     RETURNING a.tenant_id, a.trace_id, a.id, due.request_id
  ), audited AS (
    INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id,details)
      SELECT tenant_id, trace_id, 'system', 'action.cancelled', 'action', id,
             jsonb_build_object('reason','approval_claim_window_elapsed','approval_request_id',request_id)
        FROM cancelled
      RETURNING 1
  )
  SELECT count(*) FROM audited;
$$;

-- Enforce the deadline at claim time, including between watchdog ticks.
CREATE FUNCTION public.guard_approved_action_claim_window() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM public.tenant_settings s
      CROSS JOIN LATERAL (
        SELECT r.status, r.resolved_at
          FROM public.approval_requests r
         WHERE (r.tenant_id,r.case_id,r.subject_type,r.subject_id) =
               (NEW.tenant_id,NEW.case_id,'action',NEW.id)
         ORDER BY r.created_at DESC, r.id DESC LIMIT 1
      ) r
     WHERE s.tenant_id = NEW.tenant_id AND r.status = 'approved'
       AND r.resolved_at + make_interval(mins => s.approval_claim_window_minutes) > clock_timestamp()
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'action is outside its approval claim window';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER actions_validate_approval_claim_window BEFORE UPDATE OF status ON public.actions
  FOR EACH ROW WHEN (OLD.status = 'approved' AND NEW.status = 'executing')
  EXECUTE FUNCTION public.guard_approved_action_claim_window();

REVOKE ALL ON FUNCTION public.cancel_unclaimed_approved_actions(),
  public.guard_approved_action_claim_window() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cancel_unclaimed_approved_actions() TO app_core;

COMMIT;
