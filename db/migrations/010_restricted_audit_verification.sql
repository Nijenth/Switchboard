BEGIN;

REVOKE EXECUTE ON FUNCTION public.verify_audit_chain() FROM PUBLIC;

CREATE FUNCTION public.verify_audit_checkpoint(p_seq bigint, p_hash text)
RETURNS TABLE (chain_seq bigint, row_hash text, broken_at bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  entry public.audit_log%ROWTYPE;
BEGIN
  IF p_seq IS NULL OR p_seq < 0 OR p_hash IS NULL OR
     (p_seq = 0 AND p_hash <> 'GENESIS') OR (p_seq > 0 AND p_hash !~ '^[0-9a-f]{64}$') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid audit checkpoint';
  END IF;
  chain_seq := 0;
  row_hash := 'GENESIS';
  FOR entry IN SELECT * FROM public.audit_log a ORDER BY a.chain_seq LOOP
    IF entry.chain_seq <> chain_seq + 1 OR entry.prev_hash <> row_hash OR
       entry.row_hash <> public.audit_row_hash(entry.chain_seq, entry.prev_hash, entry) OR
       (entry.chain_seq = p_seq AND entry.row_hash <> p_hash) THEN
      broken_at := chain_seq + 1;
      RETURN NEXT;
      RETURN;
    END IF;
    chain_seq := entry.chain_seq;
    row_hash := entry.row_hash;
  END LOOP;
  IF chain_seq < p_seq THEN broken_at := chain_seq + 1; END IF;
  RETURN NEXT;
END $$;

REVOKE ALL ON FUNCTION public.verify_audit_checkpoint(bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.verify_audit_checkpoint(bigint,text) TO app_core;

-- These checks can fail when routing changes while ingest is using the same case.
CREATE OR REPLACE FUNCTION public.enforce_case_pack_pin() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.tenant_pack_id IS NOT NULL THEN
    IF (NEW.tenant_id, NEW.tenant_pack_id, NEW.pack_id) IS DISTINCT FROM
       (OLD.tenant_id, OLD.tenant_pack_id, OLD.pack_id) THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'case pack is already pinned';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.tenant_pack_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.tenant_packs tp
     WHERE tp.tenant_id = NEW.tenant_id AND tp.id = NEW.tenant_pack_id
       AND tp.pack_id = NEW.pack_id AND tp.enabled
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514',
      MESSAGE = 'case requires an enabled tenant pack matching the pinned version';
  END IF;
  RETURN NEW;
END $$;

COMMIT;
