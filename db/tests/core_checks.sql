\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_core',
  $q$SELECT session_user::text$q$, 'switchboard_core');

SELECT pg_temp.expect_value('core cannot assume the owner or bootstrap role',
  $q$SELECT (NOT pg_has_role('switchboard_owner', 'MEMBER')
             AND NOT pg_has_role('switchboard_bootstrap', 'MEMBER'))::text$q$, 'true');

SELECT pg_temp.expect_value('no tenant context → sees zero cases (fail closed)',
  $q$SELECT count(*)::text FROM cases$q$, '0');

SELECT pg_temp.expect_value('ingest can resolve tenant from inbound address without context',
  $q$SELECT tenant_id::text FROM resolve_channel_endpoint('email','invoices@northwind.example')$q$,
  '10000000-0000-0000-0000-000000000001');

SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT pg_temp.expect_value('Northwind context cannot see Acme''s case',
  $q$SELECT count(*)::text FROM cases WHERE id='91000000-0000-0000-0000-000000000004'$q$, '0');
SELECT pg_temp.expect_value('Northwind context sees its own case',
  $q$SELECT count(*)::text FROM cases WHERE id='92000000-0000-0000-0000-000000000001'$q$, '1');
SELECT pg_temp.expect_value('Northwind context sees no Acme staff',
  $q$SELECT count(*)::text FROM staff_users WHERE tenant_id='91000000-0000-0000-0000-000000000001'$q$, '0');

SELECT pg_temp.expect_error('cannot write a row for another tenant',
  $q$INSERT INTO messages (tenant_id, case_id, trace_id, idempotency_key, direction, channel)
     VALUES ('91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004',gen_random_uuid(),'email:rls','inbound','email')$q$,
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

\ir check_results.sql
ROLLBACK;
