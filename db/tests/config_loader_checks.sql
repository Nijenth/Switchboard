\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('loader authenticated as config login', 'SELECT session_user::text', 'switchboard_config_loader');
SELECT pg_temp.expect_value('three released packs loaded', 'SELECT count(*)::text FROM public.packs', '3');
SELECT pg_temp.expect_value('versioned ecommerce starter evals loaded',
  $q$SELECT count(*)::text FROM public.eval_cases WHERE pack_key='ecommerce_support' AND pack_version=1$q$, '61');
SELECT pg_temp.expect_value('Northwind pack knowledge loaded',
  $q$SELECT count(*)::text FROM public.knowledge_documents
      WHERE pack_key='ecommerce_support' AND source_uri LIKE 'knowledge/%'$q$, '2');
SELECT pg_temp.expect_value('pack chunks have 1536-dimensional fixture embeddings',
  $q$SELECT count(*)::text FROM public.knowledge_chunks c
      JOIN public.knowledge_documents d ON (d.tenant_id,d.id)=(c.tenant_id,c.document_id)
      WHERE d.source_uri LIKE 'knowledge/%' AND vector_dims(c.embedding)=1536
        AND c.embedding_model='offline-token-hash-v1'$q$, '2');
SELECT pg_temp.expect_value('fixture encoding ignores case, punctuation and repeated terms',
  $q$SELECT (public.offline_knowledge_embedding('Refunds, REFUNDS!')
         = public.offline_knowledge_embedding('refunds'))::text$q$, 'true');
SELECT pg_temp.expect_value('empty fixture queries have no vector',
  $q$SELECT (public.offline_knowledge_embedding('What can I do?') IS NULL)::text$q$, 'true');
SELECT pg_temp.expect_error('loader cannot read cases', 'SELECT count(*) FROM public.cases', 'permission denied');
SELECT pg_temp.expect_error('loader cannot update messages', 'UPDATE public.messages SET body_text=NULL', 'permission denied');
SELECT pg_temp.expect_error('loader cannot insert actions', 'INSERT INTO public.actions DEFAULT VALUES', 'permission denied');
SELECT pg_temp.expect_error('loader cannot rewrite a released pack', 'UPDATE public.packs SET checksum=checksum', 'permission denied');
\ir check_results.sql
ROLLBACK;
