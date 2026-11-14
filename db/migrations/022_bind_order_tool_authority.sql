BEGIN;

CREATE FUNCTION public.order_identity_input_matches(a public.actions) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(jsonb_typeof(a.input -> 'order_number') = 'string'
              AND nullif(a.input ->> 'order_number', '') IS NOT NULL
              AND jsonb_typeof(a.input -> 'sender_email') = 'string'
              AND nullif(a.input ->> 'sender_email', '') IS NOT NULL, false)
     AND EXISTS (
       SELECT 1 FROM public.messages m
        WHERE (m.tenant_id, m.case_id, m.id) = (a.tenant_id, a.case_id, a.message_id)
          AND m.direction = 'inbound'
          AND m.sender_address::public.citext = (a.input ->> 'sender_email')::public.citext
     )
$$;

CREATE OR REPLACE FUNCTION public.order_identity_is_verified(
  p_tenant uuid, p_case uuid, p_message uuid, p_order text
) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT p_message IS NOT NULL
     AND nullif(p_order, '') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM public.cases c
        WHERE (c.tenant_id, c.id) = (p_tenant, p_case) AND c.identity_verified
     )
     AND EXISTS (
       SELECT 1 FROM public.actions ic
       JOIN public.tools t ON t.key = ic.tool_key AND t.active
        WHERE (ic.tenant_id, ic.case_id, ic.message_id) = (p_tenant, p_case, p_message)
          AND ic.risk_level = 'read' AND ic.status = 'executed'
          AND coalesce(t.output_schema -> 'properties' ? 'identity_verified', false)
          AND ic.input ->> 'order_number' = p_order
          AND public.order_identity_input_matches(ic)
          AND ic.output -> 'order_found' = 'true'::jsonb
          AND ic.output -> 'identity_verified' = 'true'::jsonb
          AND NOT EXISTS (
            SELECT 1 FROM public.actions bad
            JOIN public.tools bt ON bt.key = bad.tool_key
             WHERE (bad.tenant_id, bad.case_id) = (p_tenant, p_case)
               AND bad.risk_level = 'read' AND bad.status = 'executed'
               AND coalesce(bt.output_schema -> 'properties' ? 'identity_verified', false)
               AND (bad.output -> 'identity_verified' IS DISTINCT FROM 'true'::jsonb
                    OR bad.output -> 'order_found' IS DISTINCT FROM 'true'::jsonb)
               AND (bad.message_id = p_message OR bad.executed_at >= ic.executed_at)
          )
     )
$$;

CREATE OR REPLACE FUNCTION public.order_identity_is_verified(a public.actions) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(jsonb_typeof(a.input -> 'order_number') = 'string', false)
     AND public.order_identity_is_verified(
       a.tenant_id, a.case_id, a.message_id, a.input ->> 'order_number'
     )
$$;

-- The refunded-order tools predate registry input schemas. Include them even when
-- the input omits order_number. Other tools can declare it or supply it in the action.
CREATE FUNCTION public.requires_order_identity(a public.actions, t public.tools) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT t.requires_verified_identity
     AND (a.tool_key IN ('order_lookup', 'refund_eligibility_check', 'refund_issue')
          OR coalesce(t.input_schema -> 'properties' ? 'order_number', false)
          OR a.input ? 'order_number')
$$;

-- Validate the flat registry contract used by order_lookup. Reject unsupported
-- schema keywords so a future contract change cannot silently skip validation.
CREATE FUNCTION public.valid_order_lookup_output(result jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  schema jsonb;
  field text;
  rules jsonb;
  value jsonb;
  scalar text;
BEGIN
  SELECT output_schema INTO schema FROM public.tools WHERE key = 'order_lookup' AND active;
  IF jsonb_typeof(result) IS DISTINCT FROM 'object'
     OR schema ->> 'type' IS DISTINCT FROM 'object'
     OR jsonb_typeof(schema -> 'properties') IS DISTINCT FROM 'object'
     OR jsonb_typeof(schema -> 'required') IS DISTINCT FROM 'array'
     OR schema -> 'additionalProperties' IS DISTINCT FROM 'false'::jsonb
     OR schema - ARRAY['type','properties','required','additionalProperties'] <> '{}'::jsonb THEN
    RETURN false;
  END IF;
  IF EXISTS (SELECT FROM jsonb_array_elements_text(schema -> 'required') AS r(key)
              WHERE NOT result ? r.key)
     OR EXISTS (SELECT FROM jsonb_object_keys(result) AS r(key)
                 WHERE NOT (schema -> 'properties' ? r.key)) THEN
    RETURN false;
  END IF;
  FOR field, rules IN SELECT * FROM jsonb_each(schema -> 'properties') LOOP
    IF jsonb_typeof(rules) <> 'object'
       OR rules - ARRAY['type','enum','minLength','maxLength','pattern','format'] <> '{}'::jsonb
       OR (rules ? 'format' AND rules ->> 'format' IS DISTINCT FROM 'date-time') THEN
      RETURN false;
    END IF;
    IF NOT result ? field THEN CONTINUE; END IF;
    value := result -> field;
    IF rules ? 'type' AND NOT coalesce(
         rules -> 'type' = to_jsonb(jsonb_typeof(value))
         OR (jsonb_typeof(rules -> 'type') = 'array' AND rules -> 'type' ? jsonb_typeof(value)),
         false) THEN
      RETURN false;
    END IF;
    IF rules ? 'enum' AND NOT (
        jsonb_typeof(rules -> 'enum') = 'array'
        AND rules -> 'enum' @> jsonb_build_array(value)
    ) THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(value) <> 'string' THEN CONTINUE; END IF;
    scalar := result ->> field;
    IF (rules ? 'minLength' AND length(scalar) < (rules ->> 'minLength')::int)
       OR (rules ? 'maxLength' AND length(scalar) > (rules ->> 'maxLength')::int)
       OR (rules ? 'pattern' AND NOT coalesce(scalar COLLATE "C" ~ (rules ->> 'pattern'), false))
       OR (rules ? 'format' AND NOT (
         scalar COLLATE "C" ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]+)?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$'
         AND pg_input_is_valid(left(scalar, 10), 'date')
       )) THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
END $$;

CREATE FUNCTION public.validate_order_action() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  claiming boolean := false;
  completing boolean := false;
  input_changed boolean := true;
BEGIN
  -- Runs after actions_enforce_rules, which locks the case and increments attempts at claim.
  IF TG_OP = 'UPDATE' THEN
    claiming := NEW.status = 'executing' AND NEW.attempts <> OLD.attempts;
    completing := NEW.status = 'executed' AND OLD.status <> 'executed';
    input_changed := NEW.input IS DISTINCT FROM OLD.input;
  END IF;
  IF NEW.tool_key = 'order_identity_check' AND (input_changed OR claiming OR completing)
     AND NOT public.order_identity_input_matches(NEW) THEN
    RAISE EXCEPTION USING ERRCODE = '23514',
      MESSAGE = 'order identity check requires the inbound message sender';
  END IF;
  IF claiming AND EXISTS (
    SELECT 1 FROM public.tools t
     WHERE t.key = NEW.tool_key AND public.requires_order_identity(NEW, t)
  ) AND NOT public.order_identity_is_verified(NEW) THEN
    RAISE EXCEPTION USING ERRCODE = '23514',
      MESSAGE = 'action requires verified identity for the same message and order';
  END IF;
  IF completing AND NEW.tool_key = 'order_lookup'
     AND NOT public.valid_order_lookup_output(NEW.output) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid order lookup result';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER actions_validate_order BEFORE INSERT OR UPDATE ON public.actions
  FOR EACH ROW EXECUTE FUNCTION public.validate_order_action();

REVOKE ALL ON FUNCTION public.order_identity_input_matches(public.actions),
  public.requires_order_identity(public.actions, public.tools),
  public.valid_order_lookup_output(jsonb), public.validate_order_action() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.order_identity_input_matches(public.actions),
  public.requires_order_identity(public.actions, public.tools),
  public.valid_order_lookup_output(jsonb) TO app_core;

CREATE OR REPLACE FUNCTION public.consume_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  revision bigint;
  adapter_allowed boolean;
  order_identity_required boolean;
  g public.execution_grants%ROWTYPE;
  dispatch jsonb;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' THEN RETURN NULL; END IF;
  SELECT c.policy_revision,
         tp.enabled AND tn.status = 'active' AND t.active AND t.risk_level = a.risk_level
         AND (NOT t.requires_verified_identity OR c.identity_verified)
         AND coalesce(tp.config->'adapters'->>a.tool_key, 'mock') = 'mock'
         AND a.destination_account = a.tenant_id::text,
         public.requires_order_identity(a, t)
    INTO revision, adapter_allowed, order_identity_required
    FROM public.cases c
    JOIN public.tenants tn ON tn.id = c.tenant_id
    JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id) = (c.tenant_id,c.tenant_pack_id)
    JOIN public.pack_tools pt ON (pt.pack_id, pt.tool_key) = (c.pack_id, a.tool_key)
    JOIN public.tools t ON t.key = pt.tool_key
   WHERE (c.tenant_id,c.id) = (tenant,a.case_id)
   FOR UPDATE OF c FOR SHARE OF tn, tp, pt, t;
  IF NOT FOUND OR NOT adapter_allowed OR a.adapter <> 'mock' THEN RETURN NULL; END IF;
  -- Re-evaluate under the case lock, including for grants issued before this migration.
  IF (a.tool_key = 'order_identity_check' AND NOT public.order_identity_input_matches(a))
     OR (order_identity_required AND NOT public.order_identity_is_verified(a)) THEN
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
  IF (a.tool_key = 'order_identity_check' AND NOT public.order_identity_input_matches(a))
     OR (EXISTS (SELECT 1 FROM public.tools t
                  WHERE t.key = a.tool_key AND public.requires_order_identity(a, t))
         AND NOT public.order_identity_is_verified(a)) THEN
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

COMMIT;
