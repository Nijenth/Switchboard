BEGIN;

-- The request is resolved and its decision is visible to the subject guards
-- at this point. Any refusal rolls back the whole review.
CREATE OR REPLACE FUNCTION public.apply_slack_approval_to_subject() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  reviewer uuid;
BEGIN
  SELECT reviewer_id INTO reviewer FROM public.approval_decisions
   WHERE (tenant_id,request_id,decision) = (NEW.tenant_id,NEW.id,'approved');
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Approval promotion requires a recorded decision';
  END IF;

  IF NEW.subject_type = 'action' THEN
    -- Keep the existing state machine for action reviews opened on a proposal.
    UPDATE public.actions SET status = 'pending_approval'
     WHERE (tenant_id,case_id,id,payload_hash) =
           (NEW.tenant_id,NEW.case_id,NEW.subject_id,NEW.payload_hash)
       AND status = 'proposed';
    UPDATE public.actions SET status = 'approved', approved_by = reviewer
     WHERE (tenant_id,case_id,id,payload_hash) =
           (NEW.tenant_id,NEW.case_id,NEW.subject_id,NEW.payload_hash)
       AND status = 'pending_approval';
  ELSIF NEW.subject_type = 'draft' THEN
    UPDATE public.drafts SET status = 'approved'
     WHERE (tenant_id,case_id,id,payload_hash) =
           (NEW.tenant_id,NEW.case_id,NEW.subject_id,NEW.payload_hash)
       AND status = 'pending';
  ELSE
    RETURN NEW;
  END IF;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Approval subject is no longer pending for this payload';
  END IF;
  RETURN NEW;
END $$;

ALTER FUNCTION public.apply_slack_approval_to_subject() RENAME TO promote_approved_subject;
ALTER TRIGGER approval_requests_apply_slack_approval ON public.approval_requests
  RENAME TO approval_requests_promote_approved_subject;

COMMIT;
