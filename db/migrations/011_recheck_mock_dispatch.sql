BEGIN;

CREATE OR REPLACE FUNCTION public.consume_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  revision bigint;
  adapter_allowed boolean;
  g public.execution_grants%ROWTYPE;
  dispatch jsonb;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' THEN RETURN NULL; END IF;
  SELECT c.policy_revision,
         tp.enabled AND tn.status = 'active' AND t.active AND t.risk_level = a.risk_level
         AND (NOT t.requires_verified_identity OR c.identity_verified)
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

COMMIT;
