BEGIN;

CREATE FUNCTION public.is_eligibility_tool(t public.tools) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT t.active AND t.risk_level = 'read' AND public.is_order_scoped_tool(t)
     AND coalesce(jsonb_typeof(t.output_schema -> 'properties') = 'object'
                  AND t.output_schema -> 'properties' ?& ARRAY['eligible','refundable_amount','currency'], false)
$$;

-- Serialize evidence publication and payout authority across cases for one tenant/order.
CREATE FUNCTION public.lock_order_evidence(tenant uuid, order_number text) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT pg_advisory_xact_lock(hashtextextended(tenant::text || ':' || order_number, 24024))
$$;

CREATE FUNCTION public.order_eligibility(
  tenant uuid, case_id uuid, message_id uuid, order_number text
) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE
    WHEN NOT public.valid_tool_output(t.output_schema, a.output) THEN NULL
    WHEN a.output -> 'eligible' = 'true'::jsonb AND NOT EXISTS (
      SELECT 1 FROM public.actions newer JOIN public.tools nt ON nt.key = newer.tool_key
       WHERE newer.tenant_id = a.tenant_id AND newer.status = 'executed'
         AND newer.input ->> 'order_number' = order_number
         AND (newer.executed_at, newer.id) > (a.executed_at, a.id)
         AND public.is_eligibility_tool(nt)
         AND (newer.output -> 'eligible' IS DISTINCT FROM 'true'::jsonb
              OR NOT public.valid_tool_output(nt.output_schema, newer.output))
    ) THEN a.output
    WHEN a.output -> 'eligible' IS DISTINCT FROM 'true'::jsonb THEN a.output
  END
    FROM public.actions a JOIN public.tools t ON t.key = a.tool_key
    JOIN public.cases c ON (c.tenant_id,c.id) = (a.tenant_id,a.case_id)
    JOIN public.pack_tools pt ON (pt.pack_id,pt.tool_key) = (c.pack_id,t.key)
   WHERE (a.tenant_id,a.case_id,a.message_id) = (tenant,order_eligibility.case_id,order_eligibility.message_id)
     AND a.status = 'executed' AND a.input ->> 'order_number' = order_number
     AND public.is_eligibility_tool(t) AND public.tool_identity_authorized(a,t)
   ORDER BY a.executed_at DESC, a.id DESC
   LIMIT 1
$$;

CREATE FUNCTION public.order_money_authorized(a public.actions, t public.tools) RETURNS boolean
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  evidence jsonb;
BEGIN
  IF a.risk_level <> 'money'
     OR NOT (public.is_order_scoped_tool(t) OR a.input ? 'order_number'
             OR coalesce(t.input_schema -> 'required' ? 'order_number', false)
             OR coalesce(t.input_schema -> 'properties' ? 'order_number', false)) THEN
    RETURN true;
  END IF;
  -- Migration 023 already forbids advancing these retired registry contracts.
  IF t.requires_verified_identity AND NOT public.is_order_scoped_tool(t) THEN
    RETURN a.status = 'proposed';
  END IF;
  IF NOT public.is_order_scoped_tool(t) OR NOT public.tool_identity_authorized(a,t)
     OR NOT public.valid_tool_output(t.input_schema,a.input)
     OR a.amount IS NULL OR a.amount <= 0
     OR a.currency IS DISTINCT FROM a.input ->> 'currency'
     OR (a.input ->> 'amount') IS NULL OR (a.input ->> 'amount') !~ '^[0-9]{1,10}[.][0-9]{2}$' THEN
    RETURN false;
  END IF;
  PERFORM public.lock_order_evidence(a.tenant_id,a.input ->> 'order_number');
  evidence := public.order_eligibility(a.tenant_id,a.case_id,a.message_id,a.input ->> 'order_number');
  RETURN coalesce(evidence -> 'eligible' = 'true'::jsonb
    AND evidence -> 'refundable_amount' = a.input -> 'amount'
    AND evidence -> 'currency' = a.input -> 'currency'
    AND a.amount = (a.input ->> 'amount')::numeric, false);
END $$;

CREATE UNIQUE INDEX actions_one_live_order_money
  ON public.actions (tenant_id, (input ->> 'order_number'))
  WHERE risk_level = 'money' AND input ? 'order_number'
    AND status NOT IN ('rejected','cancelled','failed');

CREATE FUNCTION public.enforce_order_money_evidence() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  t public.tools%ROWTYPE;
BEGIN
  SELECT * INTO t FROM public.tools WHERE key = NEW.tool_key;
  IF TG_OP = 'UPDATE' AND NEW.status = 'executed' AND OLD.status <> 'executed'
     AND public.is_eligibility_tool(t) THEN
    PERFORM 1 FROM public.cases WHERE (tenant_id,id) = (NEW.tenant_id, NEW.case_id) FOR UPDATE;
    IF jsonb_typeof(NEW.input -> 'order_number') IS DISTINCT FROM 'string'
       OR NOT public.tool_identity_authorized(NEW,t) THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'eligibility evidence requires verified order identity';
    END IF;
    PERFORM public.lock_order_evidence(NEW.tenant_id, NEW.input ->> 'order_number');
    NEW.executed_at := clock_timestamp();
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.status = 'executed' AND OLD.status <> 'executed'
     AND NEW.risk_level = 'money' AND NEW.input ? 'order_number'
     AND NOT public.valid_tool_output(t.output_schema,NEW.output) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid order money result';
  END IF;
  IF NEW.risk_level = 'money' AND (TG_OP = 'INSERT'
      OR (NEW.status IN ('pending_approval','approved','executing') AND NEW.status <> OLD.status)
      OR NEW.input IS DISTINCT FROM OLD.input OR NEW.amount IS DISTINCT FROM OLD.amount
      OR NEW.currency IS DISTINCT FROM OLD.currency) THEN
    PERFORM 1 FROM public.cases WHERE (tenant_id,id) = (NEW.tenant_id, NEW.case_id) FOR UPDATE;
    IF NOT public.order_money_authorized(NEW,t) THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'order money action requires matching current eligibility evidence';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER actions_validate_refund_evidence BEFORE INSERT OR UPDATE ON public.actions
  FOR EACH ROW EXECUTE FUNCTION public.enforce_order_money_evidence();

CREATE FUNCTION public.enforce_order_money_approval() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  a public.actions%ROWTYPE;
  t public.tools%ROWTYPE;
BEGIN
  SELECT * INTO a FROM public.actions
   WHERE (tenant_id,case_id,id) = (NEW.tenant_id,NEW.case_id,NEW.subject_id) AND risk_level='money'
   FOR UPDATE;
  IF NOT FOUND THEN RETURN NEW; END IF;
  SELECT * INTO STRICT t FROM public.tools WHERE key=a.tool_key;
  PERFORM 1 FROM public.cases WHERE (tenant_id,id) = (a.tenant_id,a.case_id) FOR UPDATE;
  IF NOT public.order_money_authorized(a,t) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'order money approval requires matching current eligibility evidence';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER approval_requests_validate_order_money BEFORE UPDATE OF status ON public.approval_requests
  FOR EACH ROW WHEN (NEW.subject_type = 'action' AND NEW.status = 'approved' AND OLD.status <> NEW.status)
  EXECUTE FUNCTION public.enforce_order_money_approval();

REVOKE ALL ON FUNCTION public.is_eligibility_tool(public.tools), public.lock_order_evidence(uuid,text),
  public.order_eligibility(uuid,uuid,uuid,text), public.order_money_authorized(public.actions,public.tools),
  public.enforce_order_money_evidence(), public.enforce_order_money_approval() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.is_eligibility_tool(public.tools), public.lock_order_evidence(uuid,text),
  public.order_eligibility(uuid,uuid,uuid,text), public.order_money_authorized(public.actions,public.tools) TO app_core;

CREATE TABLE demo.order_refunds (
  tenant_id uuid NOT NULL,
  order_number text NOT NULL,
  idempotency_key text NOT NULL,
  refund_reference uuid NOT NULL DEFAULT gen_random_uuid(),
  amount numeric(12,2) NOT NULL CHECK (amount > 0),
  currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  PRIMARY KEY (tenant_id,idempotency_key),
  UNIQUE (tenant_id,order_number),
  FOREIGN KEY (tenant_id,order_number) REFERENCES demo.shop_orders(tenant_id,order_number)
);
GRANT SELECT, INSERT ON demo.order_refunds TO app_n8n_mock;

CREATE FUNCTION demo.refund_eligibility(tenant uuid, requested_order text)
RETURNS TABLE (eligible boolean, reason text, refundable_amount text, currency text)
LANGUAGE sql STABLE SET search_path = pg_catalog, demo, pg_temp AS $$
  WITH eligibility AS (
    SELECT o.total_amount, o.currency, CASE
      WHEN o.id IS NULL THEN 'order_not_found'
      WHEN o.refunded_at IS NOT NULL OR o.status IN ('refunded','partially_refunded') THEN 'already_refunded'
      WHEN o.status <> 'delivered' OR o.delivered_at IS NULL OR o.delivered_at > statement_timestamp()
        THEN 'not_delivered'
      WHEN o.delivered_at < statement_timestamp() - interval '30 days' THEN 'window_expired'
      ELSE 'eligible'
    END AS reason
    FROM (SELECT 1) AS requested
    LEFT JOIN demo.shop_orders o ON (o.tenant_id,o.order_number) = (tenant,requested_order)
  )
  SELECT e.reason = 'eligible', e.reason,
         CASE WHEN e.reason = 'eligible' THEN e.total_amount::text END,
         CASE WHEN e.reason = 'eligible' THEN e.currency::text END
    FROM eligibility e
$$;

CREATE FUNCTION demo.issue_order_refund(
  tenant uuid, requested_order text, request_key text, requested_amount numeric,
  requested_currency text, expires_at timestamptz
) RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, demo, pg_temp AS $$
DECLARE
  receipt uuid;
BEGIN
  PERFORM 1 FROM demo.shop_orders o WHERE (o.tenant_id,o.order_number) = (tenant,requested_order) FOR UPDATE;
  SELECT r.refund_reference INTO receipt FROM demo.order_refunds r
   WHERE (r.tenant_id,r.idempotency_key,r.order_number,r.amount,r.currency) =
         (tenant,request_key,requested_order,requested_amount,requested_currency);
  IF FOUND THEN RETURN receipt; END IF;
  IF expires_at IS NULL OR clock_timestamp() >= expires_at THEN RETURN NULL; END IF;
  UPDATE demo.shop_orders o SET status = 'refunded', refunded_at = clock_timestamp()
   WHERE (o.tenant_id,o.order_number,o.total_amount,o.currency::text) =
         (tenant,requested_order,requested_amount,requested_currency)
     AND EXISTS (SELECT FROM demo.refund_eligibility(tenant,requested_order) WHERE eligible);
  IF NOT FOUND THEN RETURN NULL; END IF;
  INSERT INTO demo.order_refunds (tenant_id,order_number,idempotency_key,amount,currency)
  VALUES (tenant,requested_order,request_key,requested_amount,requested_currency)
  RETURNING refund_reference INTO receipt;
  RETURN receipt;
END $$;

REVOKE ALL ON FUNCTION demo.refund_eligibility(uuid,text),
  demo.issue_order_refund(uuid,text,text,numeric,text,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION demo.refund_eligibility(uuid,text),
  demo.issue_order_refund(uuid,text,text,numeric,text,timestamptz) TO app_n8n_mock;

CREATE OR REPLACE FUNCTION public.consume_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  revision bigint;
  adapter_allowed boolean;
  registry_tool public.tools%ROWTYPE;
  g public.execution_grants%ROWTYPE;
  dispatch jsonb;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' THEN RETURN NULL; END IF;
  SELECT c.policy_revision,
         tp.enabled AND tn.status = 'active' AND t.active AND t.risk_level = a.risk_level
         AND coalesce(tp.config->'adapters'->>a.tool_key, 'mock') = 'mock'
         AND a.destination_account = a.tenant_id::text
    INTO revision, adapter_allowed
    FROM public.cases c
    JOIN public.tenants tn ON tn.id = c.tenant_id
    JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id) = (c.tenant_id,c.tenant_pack_id)
    JOIN public.pack_tools pt ON (pt.pack_id, pt.tool_key) = (c.pack_id, a.tool_key)
    JOIN public.tools t ON t.key = pt.tool_key
   WHERE (c.tenant_id,c.id) = (tenant,a.case_id)
   FOR UPDATE OF c FOR SHARE OF tn, tp, pt, t;
  IF NOT FOUND OR NOT adapter_allowed OR a.adapter <> 'mock' THEN RETURN NULL; END IF;
  -- Re-evaluate under the case lock, including for grants issued before this migration.
  SELECT * INTO registry_tool FROM public.tools WHERE key = a.tool_key;
  IF NOT FOUND OR NOT public.tool_identity_authorized(a, registry_tool)
     OR NOT public.order_money_authorized(a, registry_tool) THEN
    RETURN NULL;
  END IF;
  UPDATE public.execution_grants SET consumed_at = clock_timestamp()
   WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
     AND consumed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account
     AND policy_revision = revision
   RETURNING * INTO g;
  IF NOT FOUND THEN RETURN NULL; END IF;
  dispatch := jsonb_build_object('tool_key', a.tool_key, 'adapter', a.adapter, 'destination', a.destination_account,
     'payload_hash', a.payload_hash, 'input', a.input, 'amount', a.amount, 'currency', a.currency,
     'idempotency_key', a.idempotency_key, 'expires_at', g.expires_at, 'trace_id', a.trace_id);
  IF a.execution_draft_id IS NOT NULL THEN
    SELECT dispatch || jsonb_build_object('draft', jsonb_build_object(
      'id', d.id, 'channel', d.channel, 'recipient', d.recipient, 'subject', d.subject, 'body', d.body))
      INTO dispatch FROM public.drafts d
     WHERE (d.tenant_id,d.case_id,d.id) = (tenant,a.case_id,a.execution_draft_id)
       AND d.status = 'approved' AND public.draft_is_authorized(d,revision);
    IF NOT FOUND THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'execution draft authority changed';
    END IF;
  END IF;
  RETURN dispatch;
END $$;

CREATE OR REPLACE FUNCTION public.replace_unused_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  g public.execution_grants%ROWTYPE;
  case_revision bigint;
  r public.approval_requests%ROWTYPE;
  registry_tool public.tools%ROWTYPE;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' OR a.adapter <> 'mock' OR a.claimed_by <> p_worker THEN
    RETURN NULL;
  END IF;
  SELECT * INTO g FROM public.execution_grants
   WHERE (tenant_id,action_id,attempt,worker) = (tenant,a.id,a.attempts,p_worker) FOR UPDATE;
  IF NOT FOUND OR g.consumed_at IS NOT NULL OR g.expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  SELECT policy_revision INTO case_revision FROM public.cases WHERE (tenant_id,id) = (tenant,a.case_id) FOR UPDATE;
  IF NOT FOUND OR case_revision = g.policy_revision THEN RETURN NULL; END IF;
  SELECT * INTO registry_tool FROM public.tools WHERE key = a.tool_key FOR SHARE;
  IF NOT FOUND OR NOT public.tool_identity_authorized(a, registry_tool)
     OR NOT public.order_money_authorized(a, registry_tool) THEN
    RETURN NULL;
  END IF;
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

CREATE FUNCTION public.complete_mock_money(
  p_action uuid, p_worker text, p_digest bytea, p_ref text, p_output jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
BEGIN
  IF p_ref IS NULL OR length(p_ref) NOT BETWEEN 1 AND 512 OR p_output IS NULL
     OR jsonb_typeof(p_output) <> 'object' OR octet_length(p_output::text) > 16384 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'money completion requires a reference and object output';
  END IF;
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND OR a.risk_level <> 'money' OR a.adapter <> 'mock' THEN RETURN false; END IF;
  IF a.status = 'executed' AND a.external_ref = p_ref AND a.output = p_output THEN
    RETURN EXISTS (SELECT 1 FROM public.execution_grants
      WHERE (tenant_id,action_id,attempt,worker,token_digest) = (tenant,a.id,a.attempts,p_worker,p_digest)
        AND completed_at IS NOT NULL);
  END IF;
  IF a.status <> 'executing' THEN RETURN false; END IF;
  UPDATE public.execution_grants SET completed_at = clock_timestamp()
   WHERE (tenant_id,action_id,attempt,worker,token_digest) = (tenant,a.id,a.attempts,p_worker,p_digest)
     AND consumed_at IS NOT NULL AND completed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE public.actions SET status = 'executed', external_ref = p_ref, output = p_output,
    executed_at = clock_timestamp() WHERE (tenant_id,id) = (tenant,a.id);
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id)
  VALUES (tenant,a.trace_id,'system','action.executed','action',a.id);
  RETURN true;
END $$;

CREATE FUNCTION public.reconcile_mock_action(
  p_action uuid, p_outcome text, p_lookup text, p_ref text, p_output jsonb
)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  next_status text := 'unknown_outcome';
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND OR a.adapter <> 'mock' THEN RETURN NULL; END IF;
  IF a.status = 'executing' AND a.lease_expires_at <= clock_timestamp() THEN
    UPDATE public.actions SET status = 'unknown_outcome' WHERE (tenant_id,id) = (tenant,a.id);
  ELSIF a.status <> 'unknown_outcome' THEN
    RETURN NULL;
  END IF;
  IF p_output IS NOT NULL AND p_outcome IS DISTINCT FROM 'found' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'only found evidence may carry a result';
  END IF;
  IF p_outcome = 'found' AND a.risk_level = 'money' AND a.input ? 'order_number'
     AND NOT EXISTS (
       SELECT 1 FROM public.execution_grants g
        WHERE (g.tenant_id,g.action_id,g.attempt) = (tenant,a.id,a.attempts)
          AND g.consumed_at IS NOT NULL AND g.payload_hash = a.payload_hash
     ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'found refund evidence requires a consumed grant';
  END IF;
  IF p_outcome IS NOT NULL THEN
    INSERT INTO public.reconciliation_evidence (action_id,tenant_id,attempt,outcome,lookup_ref,external_ref)
      VALUES (a.id,tenant,a.attempts,p_outcome,p_lookup,p_ref);
    IF p_outcome IN ('found','absent') THEN
      next_status := CASE WHEN p_outcome = 'found' THEN 'executed' ELSE 'failed' END;
      UPDATE public.actions SET status = next_status, external_ref = p_ref, output = p_output,
          executed_at = CASE WHEN p_outcome = 'found' THEN clock_timestamp() END
       WHERE (tenant_id,id) = (tenant,a.id);
      IF next_status = 'executed' AND a.execution_draft_id IS NOT NULL THEN
        UPDATE public.drafts SET status = 'sent' WHERE (tenant_id,id) = (tenant,a.execution_draft_id)
          AND status = 'approved';
      END IF;
    END IF;
  END IF;
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id,details)
    VALUES (tenant,a.trace_id,'system','action.reconciled','action',a.id,
            jsonb_build_object('status',next_status,'outcome',p_outcome));
  RETURN next_status;
END $$;

CREATE OR REPLACE FUNCTION public.reconcile_mock_action(p_action uuid, p_outcome text, p_lookup text, p_ref text)
RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT public.reconcile_mock_action(p_action,p_outcome,p_lookup,p_ref,NULL)
$$;

REVOKE ALL ON FUNCTION public.complete_mock_money(uuid,text,bytea,text,jsonb),
  public.reconcile_mock_action(uuid,text,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.complete_mock_money(uuid,text,bytea,text,jsonb),
  public.reconcile_mock_action(uuid,text,text,text,jsonb) TO app_core;

COMMIT;
