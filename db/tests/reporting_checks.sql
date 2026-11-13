\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_reporting',
  $q$SELECT session_user::text$q$, 'switchboard_reporting');

SELECT pg_temp.expect_error('reporting role cannot read raw messages',
  $q$SELECT count(*) FROM messages$q$, 'permission denied');
SELECT pg_temp.expect_ok('reporting role can read KPI views',
  $q$SELECT count(*) FROM v_daily_kpis$q$);

\ir check_results.sql
ROLLBACK;
