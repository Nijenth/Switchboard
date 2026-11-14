BEGIN;

-- Completes a claimed mock read action with its validated result (ADR 0019). Core validates the
-- output against the tool's registry output schema before calling this transition. A read tool
-- whose registry output schema declares `identity_verified` may verify the case's identity.
CREATE FUNCTION public.complete_mock_read(
  p_action uuid, p_worker text, p_digest bytea, p_ref text, p_output jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  declares_identity boolean;
BEGIN
  IF p_ref IS NULL OR length(p_ref) NOT BETWEEN 1 AND 512 OR p_output IS NULL
     OR jsonb_typeof(p_output) <> 'object' OR octet_length(p_output::text) > 16384 THEN
    RAISE EXCEPTION USING ERRCODE = '22023',
      MESSAGE = 'read completion requires a reference and an object output';
  END IF;
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.risk_level <> 'read' OR a.adapter <> 'mock' THEN
    RETURN false;
  END IF;
  IF a.status = 'executed' AND a.external_ref = p_ref AND a.output = p_output THEN
    RETURN EXISTS (SELECT 1 FROM public.execution_grants
       WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
         AND completed_at IS NOT NULL);
  END IF;
  IF a.status <> 'executing' THEN
    RETURN false;
  END IF;
  UPDATE public.execution_grants SET completed_at = clock_timestamp()
   WHERE (tenant_id, action_id, attempt, worker, token_digest) = (tenant, a.id, a.attempts, p_worker, p_digest)
     AND consumed_at IS NOT NULL AND completed_at IS NULL AND expires_at > clock_timestamp()
     AND payload_hash = a.payload_hash AND destination_account = a.destination_account;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE public.actions
     SET status = 'executed', external_ref = p_ref, output = p_output, executed_at = clock_timestamp()
   WHERE (tenant_id, id) = (tenant, a.id);
  INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id)
    VALUES (tenant, a.trace_id, 'system', 'action.executed', 'action', a.id);

  SELECT coalesce(t.output_schema -> 'properties' ? 'identity_verified', false)
    INTO declares_identity FROM public.tools t WHERE t.key = a.tool_key;
  IF declares_identity AND p_output -> 'identity_verified' = 'true'::jsonb THEN
    UPDATE public.cases SET identity_verified = true
     WHERE (tenant_id, id) = (tenant, a.case_id) AND NOT identity_verified;
    IF FOUND THEN
      INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id, details)
        VALUES (tenant, a.trace_id, 'system', 'case.identity_verified', 'case', a.case_id,
                jsonb_build_object('action_id', a.id, 'tool_key', a.tool_key));
    END IF;
  END IF;
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION public.complete_mock_read(uuid, text, bytea, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.complete_mock_read(uuid, text, bytea, text, jsonb) TO app_core;

COMMIT;
