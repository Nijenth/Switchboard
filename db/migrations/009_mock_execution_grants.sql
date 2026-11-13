BEGIN;

ALTER TABLE public.actions ADD CONSTRAINT actions_tenant_id_key UNIQUE (tenant_id, id);

CREATE TABLE public.execution_grants (
  token_digest bytea PRIMARY KEY CHECK (octet_length(token_digest) = 32),
  tenant_id uuid NOT NULL,
  action_id uuid NOT NULL,
  attempt integer NOT NULL,
  worker text NOT NULL,
  payload_hash text NOT NULL,
  destination_account text NOT NULL,
  policy_revision bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  completed_at timestamptz,
  UNIQUE (action_id, attempt),
  FOREIGN KEY (tenant_id, action_id) REFERENCES public.actions (tenant_id, id)
);
ALTER TABLE public.execution_grants ENABLE ROW LEVEL SECURITY;

CREATE TABLE public.reconciliation_evidence (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action_id uuid NOT NULL,
  tenant_id uuid NOT NULL,
  attempt integer NOT NULL,
  lookup_ref text NOT NULL CHECK (length(lookup_ref) BETWEEN 1 AND 512),
  outcome text NOT NULL CHECK (outcome IN ('found','absent','inconclusive')),
  external_ref text CHECK (length(external_ref) BETWEEN 1 AND 512),
  CHECK ((outcome = 'found') = (external_ref IS NOT NULL)),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, action_id) REFERENCES public.actions (tenant_id, id)
);
ALTER TABLE public.reconciliation_evidence ENABLE ROW LEVEL SECURITY;

-- Allow proposal maintenance and the human approval transition. Execution states are
-- writable by the transition functions below and never by the application role directly.
CREATE FUNCTION public.guard_execution_transitions() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF current_user <> pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid = TG_RELID))
     AND (NEW.status IN ('executing','executed','failed','unknown_outcome')
          OR OLD.status IN ('executing','executed','failed','unknown_outcome')) THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'execution requires a controlled transition';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER actions_before_execution_guard BEFORE UPDATE ON public.actions
  FOR EACH ROW EXECUTE FUNCTION public.guard_execution_transitions();

CREATE FUNCTION public.claim_mock_action(p_action uuid, p_worker text, p_digest bytea)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  claimed public.actions%ROWTYPE;
BEGIN
  IF p_worker IS NULL OR length(p_worker) NOT BETWEEN 1 AND 128 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid execution worker';
  END IF;
  UPDATE public.actions a SET status = 'executing', claimed_by = p_worker,
      lease_expires_at = clock_timestamp() + interval '45 seconds'
    WHERE (a.tenant_id, a.id) = (tenant, p_action) AND a.adapter = 'mock'
      AND a.status IN ('proposed','approved')
    RETURNING * INTO claimed;
  IF NOT FOUND THEN RETURN NULL; END IF;
  INSERT INTO public.execution_grants (token_digest, tenant_id, action_id, attempt, worker,
    payload_hash, destination_account, policy_revision, expires_at)
  SELECT p_digest, tenant, claimed.id, claimed.attempts, p_worker, claimed.payload_hash,
    claimed.destination_account, c.policy_revision, claimed.lease_expires_at
  FROM public.cases c WHERE (c.tenant_id, c.id) = (tenant, claimed.case_id);
  INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id)
  VALUES (tenant, claimed.trace_id, 'system', 'action.claimed', 'action', claimed.id);
  RETURN claimed.lease_expires_at;
END $$;

CREATE FUNCTION public.consume_mock_grant(p_action uuid, p_worker text, p_digest bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
  revision bigint;
  enabled boolean;
  g public.execution_grants%ROWTYPE;
  dispatch jsonb;
BEGIN
  SELECT * INTO a FROM public.actions WHERE (tenant_id, id) = (tenant, p_action) FOR UPDATE;
  IF NOT FOUND OR a.status <> 'executing' THEN RETURN NULL; END IF;
  SELECT c.policy_revision, tp.enabled INTO revision, enabled
    FROM public.cases c JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id) = (c.tenant_id,c.tenant_pack_id)
   WHERE (c.tenant_id,c.id) = (tenant,a.case_id) FOR UPDATE OF c;
  IF NOT FOUND OR NOT enabled OR a.adapter <> 'mock' THEN RETURN NULL; END IF;
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

CREATE FUNCTION public.complete_mock_action(p_action uuid, p_worker text, p_digest bytea, p_ref text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  tenant uuid := public.app_current_tenant();
  a public.actions%ROWTYPE;
BEGIN
  IF p_ref IS NULL OR length(p_ref) NOT BETWEEN 1 AND 512 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'execution requires an external reference';
  END IF;
  SELECT * INTO a FROM public.actions WHERE (tenant_id,id) = (tenant,p_action) FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  IF a.status = 'executed' AND a.external_ref = p_ref THEN
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
  UPDATE public.actions SET status = 'executed', external_ref = p_ref, executed_at = clock_timestamp()
   WHERE (tenant_id,id) = (tenant,a.id);
  IF a.execution_draft_id IS NOT NULL THEN
    UPDATE public.drafts SET status = 'sent' WHERE (tenant_id,id) = (tenant,a.execution_draft_id)
      AND status = 'approved';
  END IF;
  INSERT INTO public.audit_log (tenant_id,trace_id,actor_type,event_type,entity_type,entity_id)
    VALUES (tenant,a.trace_id,'system','action.executed','action',a.id);
  RETURN true;
END $$;

CREATE FUNCTION public.reconcile_mock_action(p_action uuid, p_outcome text, p_lookup text, p_ref text)
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
  IF p_outcome IS NOT NULL THEN
    INSERT INTO public.reconciliation_evidence (action_id,tenant_id,attempt,outcome,lookup_ref,external_ref)
      VALUES (a.id,tenant,a.attempts,p_outcome,p_lookup,p_ref);
    IF p_outcome IN ('found','absent') THEN
      next_status := CASE WHEN p_outcome = 'found' THEN 'executed' ELSE 'failed' END;
      UPDATE public.actions SET status = next_status, external_ref = p_ref,
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

REVOKE ALL ON FUNCTION public.claim_mock_action(uuid,text,bytea), public.consume_mock_grant(uuid,text,bytea),
  public.complete_mock_action(uuid,text,bytea,text), public.reconcile_mock_action(uuid,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_mock_action(uuid,text,bytea), public.consume_mock_grant(uuid,text,bytea),
  public.complete_mock_action(uuid,text,bytea,text), public.reconcile_mock_action(uuid,text,text,text) TO app_core;

COMMIT;
