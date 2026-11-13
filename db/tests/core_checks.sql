\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_core',
  $q$SELECT session_user::text$q$, 'switchboard_core');

SELECT pg_temp.expect_value('core cannot assume the owner or bootstrap role',
  $q$SELECT (NOT pg_has_role('switchboard_owner', 'MEMBER')
             AND NOT pg_has_role('switchboard_bootstrap', 'MEMBER'))::text$q$, 'true');

SELECT pg_temp.expect_value('no tenant context → sees zero cases (fail closed)',
  $q$SELECT count(*)::text FROM cases$q$, '0');
SELECT pg_temp.expect_error('understand claim requires tenant context',
  $q$INSERT INTO understand_requests (tenant_id,case_id,message_id,pack_id,rerun_key)
      VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',
              '92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003',
              gen_random_uuid())$q$, 'row-level security');

SELECT pg_temp.expect_value('ingest can resolve tenant from its credential without context',
  $q$SELECT tenant_id::text FROM resolve_channel_endpoint(sha256(convert_to('invoice-fixture','UTF8')))$q$,
  '10000000-0000-0000-0000-000000000001');

SELECT pg_temp.expect_value('resolver returns the enabled pack version',
  $q$SELECT pack_id::text FROM resolve_channel_endpoint(sha256(convert_to('invoice-fixture','UTF8')))$q$,
  '30000000-0000-0000-0000-000000000003');
SELECT pg_temp.expect_value('resolver rejects a disabled mapped pack',
  $q$SELECT count(*)::text FROM resolve_channel_endpoint(sha256(convert_to('disabled-fixture','UTF8')))$q$, '0');
SELECT pg_temp.expect_value('resolver allows a shared inbox without a pack',
  $q$SELECT (pack_id IS NULL AND tenant_pack_id IS NULL)::text
       FROM resolve_channel_endpoint(sha256(convert_to('shared-fixture','UTF8')))$q$, 'true');
SELECT pg_temp.expect_value('resolver rejects an unknown credential',
  $q$SELECT count(*)::text FROM resolve_channel_endpoint(sha256(convert_to('unknown-fixture','UTF8')))$q$, '0');
SELECT pg_temp.expect_value('resolver rejects an expired credential',
  $q$SELECT count(*)::text FROM resolve_channel_endpoint(sha256(convert_to('expired-fixture','UTF8')))$q$, '0');
SELECT pg_temp.expect_value('resolver rejects a revoked credential',
  $q$SELECT count(*)::text FROM resolve_channel_endpoint(sha256(convert_to('revoked-fixture','UTF8')))$q$, '0');
SELECT pg_temp.expect_error('core cannot read credential digests',
  $q$SELECT count(*) FROM ingress_credentials$q$, 'permission denied');
SELECT pg_temp.expect_value('privileged functions use a trusted search path',
  $q$SELECT count(*)::text FROM pg_proc
      WHERE oid IN ('public.resolve_channel_endpoint(bytea)'::regprocedure,
                    'public.audit_log_chain()'::regprocedure)
        AND 'search_path=pg_catalog, public, pg_temp' = ANY(proconfig)$q$, '2');

SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT pg_temp.expect_error('understand claim requires the pinned case pack',
  $q$INSERT INTO understand_requests (tenant_id,case_id,message_id,pack_id,rerun_key)
      VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',
              '92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000001',
              gen_random_uuid())$q$, 'foreign key constraint');
SELECT pg_temp.expect_value('Northwind context cannot see Acme''s case',
  $q$SELECT count(*)::text FROM cases WHERE id='91000000-0000-0000-0000-000000000004'$q$, '0');
SELECT pg_temp.expect_value('Northwind context sees its own case',
  $q$SELECT count(*)::text FROM cases WHERE id='92000000-0000-0000-0000-000000000001'$q$, '1');
SELECT pg_temp.expect_value('Northwind context sees no Acme staff',
  $q$SELECT count(*)::text FROM staff_users WHERE tenant_id='91000000-0000-0000-0000-000000000001'$q$, '0');

SELECT pg_temp.expect_error('cannot write a row for another tenant',
  $q$INSERT INTO messages (tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel)
     VALUES ('91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004','91000000-0000-0000-0000-000000000005',gen_random_uuid(),'email:rls','inbound','email')$q$,
  'row-level security');

SELECT pg_temp.expect_error('app cannot change which tools a pack may use',
  $q$UPDATE pack_tools SET approval_role = NULL WHERE tool_key = 'invoice_schedule_payment'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot change a tool''s risk level',
  $q$UPDATE tools SET risk_level = 'read' WHERE key = 'refund_issue'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot change tenant pack config',
  $q$UPDATE tenant_packs SET config = '{}'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot edit audit records',
  $q$UPDATE audit_log SET actor_id = 'x'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot delete operational data',
  $q$DELETE FROM cases WHERE id='92000000-0000-0000-0000-000000000001'$q$, 'permission denied');

SELECT pg_temp.expect_ok('app can append to the audit log',
  $q$INSERT INTO audit_log (tenant_id, actor_type, actor_id, event_type)
     VALUES ('10000000-0000-0000-0000-000000000001','system','test','test.event')$q$);
\ir approval_integrity_checks.sql
SELECT pg_temp.expect_value('restricted verifier reads the full chain under core RLS',
  $q$SELECT (chain_seq > 0 AND length(row_hash) = 64 AND broken_at IS NULL)::text
       FROM public.verify_audit_checkpoint(0,'GENESIS')$q$, 'true');
COMMIT;

\ir tenant_context_checks.sql

\ir check_results.sql
