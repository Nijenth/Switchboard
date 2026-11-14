\ir check_helpers.sql
BEGIN;
SELECT pg_temp.expect_value('authenticated as switchboard_n8n_mock',
  $q$SELECT session_user::text$q$, 'switchboard_n8n_mock');

SELECT pg_temp.expect_error('n8n mock role cannot read engine tables',
  $q$SELECT count(*) FROM cases$q$, 'permission denied');
SELECT pg_temp.expect_value('n8n mock role can read the mock shop',
  $q$SELECT count(*)::text FROM demo.shop_orders WHERE order_number='NW-1001'$q$, '1');

SELECT pg_temp.expect_value('mock eligibility covers each order outcome',
  $q$SELECT string_agg(e.reason,',' ORDER BY o.number)
     FROM (VALUES ('NW-1001'),('NW-1003'),('NW-1004'),('NW-1007'),('NW-9999')) o(number)
     CROSS JOIN LATERAL demo.refund_eligibility('10000000-0000-0000-0000-000000000001',o.number) e$q$,
  'not_delivered,eligible,window_expired,already_refunded,order_not_found');
SELECT pg_temp.expect_value('mock refund rejects a different amount',
  $q$SELECT (demo.issue_order_refund('10000000-0000-0000-0000-000000000001','NW-1003','wrong-amount',
                                      1,'EUR',clock_timestamp()+interval '1 minute') IS NULL)::text$q$,
  'true');
SELECT pg_temp.expect_value('mock refund records the approved amount',
  $q$SELECT (demo.issue_order_refund('10000000-0000-0000-0000-000000000001','NW-1003','fixture-refund',
                                      210,'EUR',clock_timestamp()+interval '1 minute') IS NOT NULL)::text$q$,
  'true');
SELECT pg_temp.expect_value('mock refund repeats return the exact receipt',
  $q$SELECT (demo.issue_order_refund('10000000-0000-0000-0000-000000000001','NW-1003','fixture-refund',
                                      210,'EUR',clock_timestamp()-interval '1 minute') = refund_reference)::text
       FROM demo.order_refunds WHERE idempotency_key='fixture-refund'$q$, 'true');
SELECT pg_temp.expect_value('mock refund prevents a second payout under a new key',
  $q$SELECT (demo.issue_order_refund('10000000-0000-0000-0000-000000000001','NW-1003','duplicate',
                                      210,'EUR',clock_timestamp()+interval '1 minute') IS NULL)::text$q$,
  'true');

\ir check_results.sql
ROLLBACK;
