BEGIN;

CREATE FUNCTION public.draft_guardrails_pass(d public.drafts) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT d.guardrail_results @> '{"g6_passed":true,"g7_passed":true}'::jsonb
     AND CASE WHEN jsonb_typeof(d.citations) = 'array'
              THEN jsonb_array_length(d.citations) > 0 ELSE false END
$$;
REVOKE ALL ON FUNCTION public.draft_guardrails_pass(public.drafts) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.draft_guardrails_pass(public.drafts) TO app_core;

CREATE FUNCTION public.require_auto_draft_guardrails() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.decision = 'auto' AND (
       NEW.draft_id IS NOT NULL OR 'send_reply' = ANY(NEW.allowed_tools)
     ) AND NOT EXISTS (
       SELECT 1 FROM public.drafts d
       WHERE (d.tenant_id, d.case_id, d.id, d.payload_hash) =
             (NEW.tenant_id, NEW.case_id, NEW.draft_id, NEW.draft_payload_hash)
         AND public.draft_guardrails_pass(d)
     ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514',
      MESSAGE = 'automatic outbound policy requires citations and passing G6 and G7 on the exact draft';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER policy_decisions_require_auto_draft_guardrails
BEFORE INSERT ON public.policy_decisions
FOR EACH ROW EXECUTE FUNCTION public.require_auto_draft_guardrails();
REVOKE ALL ON FUNCTION public.require_auto_draft_guardrails() FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.draft_is_authorized(d public.drafts, current_revision bigint)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.policy_decisions pd
    WHERE (pd.tenant_id, pd.case_id, pd.id) = (d.tenant_id, d.case_id, d.policy_decision_id)
      AND (
        (pd.revision = current_revision AND pd.decision = 'auto'
         AND pd.draft_id = d.id AND pd.draft_payload_hash = d.payload_hash
         AND public.draft_guardrails_pass(d))
        OR EXISTS (
          SELECT 1 FROM public.approval_requests r
          JOIN public.approval_decisions ad ON (ad.tenant_id, ad.request_id) = (r.tenant_id, r.id)
          WHERE (r.tenant_id, r.case_id, r.subject_type, r.subject_id, r.payload_hash) =
                (d.tenant_id, d.case_id, 'draft', d.id, d.payload_hash)
            AND r.status = 'approved' AND ad.decision = 'approved'
            AND NOT EXISTS (
              SELECT 1 FROM public.approval_requests newer
               WHERE (newer.tenant_id, newer.case_id, newer.subject_type, newer.subject_id) =
                     (r.tenant_id, r.case_id, r.subject_type, r.subject_id)
                 AND (newer.created_at, newer.id) > (r.created_at, r.id)
            )
            AND public.approval_policy_is_current(r, current_revision)
        )
      )
  )
$$;

COMMIT;
