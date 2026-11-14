BEGIN;

CREATE FUNCTION public.offline_knowledge_embedding(input_text text) RETURNS public.vector
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
  WITH terms AS (
    SELECT DISTINCT token
    FROM regexp_split_to_table(lower(input_text COLLATE "C"), '[^a-z0-9]+') AS token
    WHERE length(token) >= 3
      AND token NOT IN ('the','and','for','with','from','this','that','are','was','can','how','what',
                        'when','where','please','your','you','have','our','has','may','will','not')
  ), hashed AS (
    SELECT sha256(convert_to(token, 'UTF8')) AS digest FROM terms
  ), buckets AS (
    SELECT DISTINCT (get_byte(digest, 0) * 256 + get_byte(digest, 1)) % 1536 AS slot
    FROM hashed
  )
  SELECT CASE WHEN count(b.slot) > 0 THEN
    array_agg(CASE WHEN b.slot IS NULL THEN 0::real ELSE 1::real END ORDER BY i)::public.vector
  END
  FROM generate_series(0, 1535) AS i LEFT JOIN buckets b ON b.slot = i
$$;

REVOKE ALL ON FUNCTION public.offline_knowledge_embedding(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.offline_knowledge_embedding(text) TO app_core, app_config_loader;

UPDATE public.knowledge_chunks c
SET embedding = public.offline_knowledge_embedding(c.content), embedding_model = 'offline-token-hash-v1'
FROM public.knowledge_documents d
WHERE (d.tenant_id,d.id,d.pack_key) = (c.tenant_id,c.document_id,c.pack_key)
  AND d.source_uri LIKE 'knowledge/%' AND c.embedding IS NULL AND c.embedding_model IS NULL;

COMMIT;
