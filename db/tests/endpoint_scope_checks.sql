INSERT INTO channel_endpoints (id, tenant_id, channel, address) VALUES
  ('96000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001','email','one@scope.example'),
  ('96000000-0000-0000-0000-000000000002','10000000-0000-0000-0000-000000000001','email','two@scope.example');

SELECT pg_temp.expect_ok('same provider thread ID is independent across endpoints',
  $q$INSERT INTO cases (id, tenant_id, channel, endpoint_id, external_thread_id) VALUES
     ('96000000-0000-0000-0000-000000000003','10000000-0000-0000-0000-000000000001','email','96000000-0000-0000-0000-000000000001','endpoint-thread'),
     ('96000000-0000-0000-0000-000000000004','10000000-0000-0000-0000-000000000001','email','96000000-0000-0000-0000-000000000002','endpoint-thread')$q$);
SELECT pg_temp.expect_error('same endpoint cannot have duplicate provider threads',
  $q$INSERT INTO cases (tenant_id, channel, endpoint_id, external_thread_id)
     VALUES ('10000000-0000-0000-0000-000000000001','email','96000000-0000-0000-0000-000000000001','endpoint-thread')$q$,
  'cases_source_thread_key');
SELECT pg_temp.expect_error('unscoped cases retain thread uniqueness',
  $q$INSERT INTO cases (tenant_id, channel, external_thread_id)
     VALUES ('10000000-0000-0000-0000-000000000001','email','nw-th-1')$q$, 'cases_source_thread_key');
SELECT pg_temp.expect_error('case channel must match its endpoint',
  $q$INSERT INTO cases (tenant_id, channel, endpoint_id)
     VALUES ('10000000-0000-0000-0000-000000000001','web_form','96000000-0000-0000-0000-000000000001')$q$,
  'cases_endpoint_source_fk');

SELECT pg_temp.expect_ok('same provider message key is independent across endpoints',
  $q$INSERT INTO messages (tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel) VALUES
     ('10000000-0000-0000-0000-000000000001','96000000-0000-0000-0000-000000000003','96000000-0000-0000-0000-000000000001',gen_random_uuid(),'endpoint-message','inbound','email'),
     ('10000000-0000-0000-0000-000000000001','96000000-0000-0000-0000-000000000004','96000000-0000-0000-0000-000000000002',gen_random_uuid(),'endpoint-message','inbound','email')$q$);
SELECT pg_temp.expect_error('same endpoint cannot have duplicate message keys',
  $q$INSERT INTO messages (tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','96000000-0000-0000-0000-000000000003','96000000-0000-0000-0000-000000000001',gen_random_uuid(),'endpoint-message','inbound','email')$q$,
  'messages_source_idempotency_key');
SELECT pg_temp.expect_error('message endpoint must match its case',
  $q$INSERT INTO messages (tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','96000000-0000-0000-0000-000000000003','96000000-0000-0000-0000-000000000002',gen_random_uuid(),'wrong-endpoint','inbound','email')$q$,
  'messages_case_source_fk');
