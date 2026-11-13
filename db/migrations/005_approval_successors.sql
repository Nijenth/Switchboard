BEGIN;

-- The decision function is the only application path that can insert a decision.
CREATE FUNCTION public.apply_approval_disposition() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  req public.approval_requests%ROWTYPE;
  action_row public.actions%ROWTYPE;
  draft_row public.drafts%ROWTYPE;
  successor uuid;
  successor_request uuid;
  edits jsonb := NEW.edited_payload;
BEGIN
  IF NEW.decision <> 'edited' AND edits IS NOT NULL THEN
    RAISE EXCEPTION 'only an edited decision can include replacement fields';
  END IF;
  IF NEW.decision = 'approved' THEN
    RETURN NEW;
  END IF;
  SELECT * INTO STRICT req FROM public.approval_requests
   WHERE (tenant_id, id) = (NEW.tenant_id, NEW.request_id);

  IF req.subject_type = 'action' THEN
    UPDATE public.actions SET status = CASE WHEN NEW.decision = 'rejected' THEN 'rejected' ELSE 'cancelled' END
     WHERE (tenant_id, case_id, id, payload_hash) =
           (req.tenant_id, req.case_id, req.subject_id, req.payload_hash)
       AND status IN ('proposed', 'pending_approval')
     RETURNING * INTO action_row;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'approval action is no longer pending';
    END IF;
    IF NEW.decision = 'rejected' THEN
      RETURN NEW;
    END IF;
    IF NEW.decision = 'edited' AND (
      jsonb_typeof(edits) IS DISTINCT FROM 'object'
      OR edits = '{}'::jsonb OR edits - ARRAY['amount','currency'] <> '{}'::jsonb
      OR (edits ? 'amount' AND (jsonb_typeof(edits->'amount') <> 'number'
                                OR (edits->>'amount')::numeric < 0
                                OR (edits->>'amount')::numeric <> round((edits->>'amount')::numeric, 2)))
      OR (edits ? 'currency' AND (jsonb_typeof(edits->'currency') <> 'string'
                                  OR edits->>'currency' !~ '^[A-Z]{3}$'))
    ) THEN
      RAISE EXCEPTION 'action edit must contain a valid amount or currency';
    END IF;
    INSERT INTO public.actions (tenant_id, case_id, message_id, policy_decision_id, trace_id,
                                idempotency_key, tool_key, risk_level, adapter, amount, currency, input, status)
      VALUES (req.tenant_id, req.case_id, action_row.message_id, action_row.policy_decision_id,
              action_row.trace_id, gen_random_uuid()::text, action_row.tool_key, action_row.risk_level,
              action_row.adapter, coalesce((edits->>'amount')::numeric, action_row.amount),
              coalesce(edits->>'currency', action_row.currency), action_row.input,
              'pending_approval')
      RETURNING id INTO successor;
  ELSIF req.subject_type = 'draft' THEN
    UPDATE public.drafts SET status = CASE WHEN NEW.decision = 'rejected' THEN 'rejected' ELSE 'superseded' END
     WHERE (tenant_id, case_id, id, payload_hash) =
           (req.tenant_id, req.case_id, req.subject_id, req.payload_hash)
       AND status = 'pending'
     RETURNING * INTO draft_row;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'approval draft is no longer pending';
    END IF;
    IF NEW.decision = 'rejected' THEN
      RETURN NEW;
    END IF;
    IF NEW.decision = 'edited' AND (
      jsonb_typeof(edits) IS DISTINCT FROM 'object'
      OR edits = '{}'::jsonb OR edits - ARRAY['subject','body'] <> '{}'::jsonb
      OR (edits ? 'body' AND (jsonb_typeof(edits->'body') <> 'string' OR btrim(edits->>'body') = ''))
      OR (edits ? 'subject' AND jsonb_typeof(edits->'subject') NOT IN ('string','null'))
    ) THEN
      RAISE EXCEPTION 'draft edit must contain validated subject or body fields';
    END IF;
    INSERT INTO public.drafts (tenant_id, case_id, in_reply_to_message_id, policy_decision_id,
                               channel, recipient, subject, body, citations, guardrail_results)
      VALUES (req.tenant_id, req.case_id, draft_row.in_reply_to_message_id, draft_row.policy_decision_id,
              draft_row.channel, draft_row.recipient,
              CASE WHEN edits ? 'subject' THEN edits->>'subject' ELSE draft_row.subject END,
              coalesce(edits->>'body', draft_row.body), draft_row.citations,
              CASE WHEN NEW.decision = 'edited' THEN '{}'::jsonb ELSE draft_row.guardrail_results END)
      RETURNING id INTO successor;
  ELSIF NEW.decision IN ('edited', 'escalated') THEN
    RAISE EXCEPTION 'only action and draft requests support replacement';
  ELSE
    RETURN NEW;
  END IF;

  INSERT INTO public.approval_requests (tenant_id, case_id, subject_type, subject_id, reason)
    VALUES (req.tenant_id, req.case_id, req.subject_type, successor, 'Replacement following ' || NEW.decision)
    RETURNING id INTO successor_request;
  IF NEW.decision = 'escalated' OR req.required_role = 'admin' THEN
    UPDATE public.approval_requests SET required_role = 'admin'
     WHERE (tenant_id, id) = (req.tenant_id, successor_request);
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER approval_decisions_apply_disposition AFTER INSERT ON public.approval_decisions
  FOR EACH ROW EXECUTE FUNCTION public.apply_approval_disposition();
REVOKE ALL ON FUNCTION public.apply_approval_disposition() FROM PUBLIC;

COMMIT;
