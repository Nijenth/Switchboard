BEGIN;

-- Acquire the same row lock as a payload update before the capture trigger reads it.
CREATE FUNCTION public.lock_approval_subject() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  CASE NEW.subject_type
    WHEN 'action' THEN
      PERFORM 1 FROM public.actions
       WHERE (tenant_id, case_id, id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id)
         AND status IN ('proposed', 'pending_approval') FOR NO KEY UPDATE;
    WHEN 'draft' THEN
      PERFORM 1 FROM public.drafts
       WHERE (tenant_id, case_id, id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id)
         AND status = 'pending' FOR NO KEY UPDATE;
    WHEN 'extraction' THEN
      PERFORM 1 FROM public.extractions
       WHERE (tenant_id, case_id, id) = (NEW.tenant_id, NEW.case_id, NEW.subject_id) FOR NO KEY UPDATE;
    ELSE
      RAISE EXCEPTION 'unsupported approval subject';
  END CASE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'pending approval subject not found in this tenant/case';
  END IF;
  RETURN NEW;
END $$;

-- PostgreSQL runs same-event triggers alphabetically. Lock before capturing the payload.
CREATE TRIGGER approval_requests_before_capture BEFORE INSERT ON public.approval_requests
  FOR EACH ROW EXECUTE FUNCTION public.lock_approval_subject();

COMMIT;
