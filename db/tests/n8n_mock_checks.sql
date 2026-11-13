\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_n8n_mock',
  $q$SELECT session_user::text$q$, 'switchboard_n8n_mock');

SELECT pg_temp.expect_error('n8n mock role cannot read engine tables',
  $q$SELECT count(*) FROM cases$q$, 'permission denied');
SELECT pg_temp.expect_value('n8n mock role can read the mock shop',
  $q$SELECT count(*)::text FROM demo.shop_orders WHERE order_number='NW-1001'$q$, '1');

\ir check_results.sql
ROLLBACK;
