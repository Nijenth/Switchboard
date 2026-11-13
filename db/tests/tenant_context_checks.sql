-- Reuse this authenticated connection across transaction boundaries.
SELECT pg_temp.expect_value('tenant context clears after commit',
  $q$SELECT count(*)::text FROM cases$q$, '0');

BEGIN;
SELECT set_config('app.tenant_id', '91000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT pg_temp.expect_value('reused connection sees the second tenant only',
  $q$SELECT coalesce(array_agg(DISTINCT tenant_id)::text, '') FROM cases$q$,
  '{91000000-0000-0000-0000-000000000001}');
COMMIT;
SELECT pg_temp.expect_value('tenant context clears after the second tenant commits',
  $q$SELECT count(*)::text FROM cases$q$, '0');

BEGIN;
SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT count(*) > 0 AS tenant_context_was_active FROM cases \gset
ROLLBACK;
SELECT pg_temp.record(:'tenant_context_was_active'::boolean, 'tenant context was active before rollback');
SELECT pg_temp.expect_value('tenant context clears after rollback',
  $q$SELECT count(*)::text FROM cases$q$, '0');

BEGIN;
SELECT set_config('app.tenant_id', 'invalid-tenant-context', true) \g /dev/null
SELECT pg_temp.expect_error('malformed tenant context fails closed',
  $q$SELECT count(*) FROM cases$q$, 'invalid input syntax for type uuid');
COMMIT;
SELECT pg_temp.expect_value('connection is usable after malformed context clears',
  $q$SELECT count(*)::text FROM cases$q$, '0');
