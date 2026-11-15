\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_reporting',
  $q$SELECT session_user::text$q$, 'switchboard_reporting');

SELECT pg_temp.expect_error('reporting role cannot read raw messages',
  $q$SELECT count(*) FROM messages$q$, 'permission denied');
SELECT pg_temp.expect_error('reporting cannot resolve ingress credentials',
  $q$SELECT count(*) FROM resolve_channel_endpoint(sha256(convert_to('invoice-fixture','UTF8')))$q$, 'permission denied');
SELECT pg_temp.expect_error('reporting cannot use the full-chain verifier',
  $q$SELECT * FROM public.verify_audit_checkpoint(0,'GENESIS')$q$, 'permission denied');
SELECT pg_temp.expect_ok('reporting role can read KPI views',
  $q$SELECT count(*) FROM v_daily_kpis$q$);
SELECT pg_temp.expect_value('reporting role can read a seeded case timeline event',
  $q$SELECT count(*)::text FROM public.v_case_timeline
      WHERE case_id='92000000-0000-0000-0000-000000000001' AND event_kind='message'$q$, '1');
SELECT pg_temp.expect_error('timeline cannot expose the raw message body column',
  $q$SELECT body_text FROM public.v_case_timeline$q$, 'does not exist');
SELECT pg_temp.expect_value('the reporting-only test cluster has no disposable text view',
  $q$SELECT (to_regclass('public.demo_case_timeline') IS NULL)::text$q$, 'true');

\ir check_results.sql
ROLLBACK;
