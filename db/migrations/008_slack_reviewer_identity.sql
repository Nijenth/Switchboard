BEGIN;

ALTER TABLE public.tenant_settings ADD COLUMN slack_workspace_id text;
CREATE UNIQUE INDEX staff_users_tenant_slack_user_key
  ON public.staff_users (tenant_id, slack_user_id) WHERE slack_user_id IS NOT NULL;

CREATE FUNCTION public.record_slack_approval(
  p_request uuid, p_workspace text, p_user text, p_channel text, p_message_ts text, p_decision text
) RETURNS TABLE (decision_id uuid, tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  req public.approval_requests%ROWTYPE;
  reviewer uuid;
  prior_decision uuid;
  saved_context text;
BEGIN
  IF p_decision IS NULL OR p_decision NOT IN ('approved','rejected','escalated') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unsupported Slack decision';
  END IF;
  SELECT r.* INTO req FROM public.approval_requests r
  JOIN public.tenant_settings s ON s.tenant_id = r.tenant_id
  JOIN public.tenants t ON t.id = r.tenant_id AND t.status = 'active'
  WHERE r.id = p_request AND s.slack_workspace_id = p_workspace
    AND r.slack_channel = p_channel AND r.slack_message_ts = p_message_ts
  FOR UPDATE OF r;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Slack interaction does not match an approval';
  END IF;
  SELECT id INTO reviewer FROM public.staff_users staff
   WHERE staff.tenant_id = req.tenant_id AND staff.slack_user_id = p_user AND staff.active;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Slack reviewer is not registered';
  END IF;
  SELECT ad.id INTO prior_decision FROM public.approval_decisions ad
   WHERE ad.tenant_id = req.tenant_id AND ad.request_id = req.id
     AND ad.reviewer_id = reviewer AND ad.decision = p_decision AND ad.channel = 'slack';
  IF FOUND THEN
    RETURN QUERY SELECT prior_decision, req.tenant_id;
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.staff_users staff WHERE staff.id = reviewer AND staff.tenant_id = req.tenant_id
      AND (staff.role IN ('admin', req.required_role)
           OR (req.required_role = 'agent' AND staff.role IN ('manager','finance')))
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Slack reviewer lacks the required role';
  END IF;
  IF req.status <> 'pending' OR clock_timestamp() >= req.sla_due_at THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'approval is already resolved or past its SLA';
  END IF;
  saved_context := current_setting('app.tenant_id', true);
  PERFORM set_config('app.tenant_id', req.tenant_id::text, true);
  decision_id := public.record_approval_decision(req.id, reviewer, p_decision, 'slack');
  tenant_id := req.tenant_id;
  PERFORM set_config('app.tenant_id', coalesce(saved_context, ''), true);
  RETURN NEXT;
END $$;

REVOKE ALL ON FUNCTION public.record_slack_approval(uuid, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_slack_approval(uuid, text, text, text, text, text) TO app_core;

COMMIT;
