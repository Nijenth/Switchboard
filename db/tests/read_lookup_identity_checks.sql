-- Read tool results (ADR 0019, migration 020) and order-scoped identity / lookup recovery
-- (ADR 0021, migration 021).

SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null

INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, endpoint_id)
SELECT '9c000000-0000-0000-0000-000000000001', e.tenant_id, e.tenant_pack_id, tp.pack_id, 'email', e.id
  FROM channel_endpoints e JOIN tenant_packs tp ON (tp.tenant_id, tp.id) = (e.tenant_id, e.tenant_pack_id)
 WHERE e.channel = 'email' AND e.address = 'support@northwind.example';

INSERT INTO messages (id, tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction,
                      channel, body_text)
SELECT msg.id, c.tenant_id, c.id, c.endpoint_id, gen_random_uuid(), msg.key, 'inbound', 'email', msg.body
  FROM cases c CROSS JOIN (VALUES
    ('9c000000-0000-0000-0000-000000000002'::uuid, 'lookup-msg-1', 'Where is NW-1001?'),
    ('9c000000-0000-0000-0000-000000000003'::uuid, 'lookup-msg-2', 'What about NW-1002?')
  ) msg(id, key, body)
 WHERE c.id = '9c000000-0000-0000-0000-000000000001';

SELECT pg_temp.expect_error('order_lookup cannot be proposed before order_identity_check succeeds',
  $q$INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                          risk_level, input, status)
     VALUES ('10000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000001',
             '9c000000-0000-0000-0000-000000000002', gen_random_uuid(), 'premature-order-lookup',
             'order_lookup', 'read', '{"order_number":"NW-1001"}', 'proposed')$q$,
  'requires verified identity for the same message and order');

INSERT INTO actions (id, tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                     risk_level, input, status)
VALUES ('9c000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000001',
        '9c000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000002',
        gen_random_uuid(), 'id-check-msg-1', 'order_identity_check', 'read',
        '{"order_number":"NW-1001","sender_email":"emma.jones@example.com"}', 'proposed');

SELECT pg_temp.expect_value('first claim on order_identity_check succeeds',
  $q$SELECT (public.claim_mock_action('9c000000-0000-0000-0000-000000000004', 'w1',
             sha256('grant-ic-1'::bytea)) IS NOT NULL)::text$q$, 'true');

SELECT pg_temp.expect_value('active executing read action cannot be reclaimed before lease expiry',
  $q$SELECT (public.claim_mock_action('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-1b'::bytea)) IS NULL)::text$q$, 'true');

-- Expire the lease and prove the stuck executing read action is recoverable via claim_mock_action.
ALTER TABLE public.actions DISABLE TRIGGER actions_enforce_rules;
UPDATE public.actions SET lease_expires_at = clock_timestamp() - interval '1 second'
 WHERE id = '9c000000-0000-0000-0000-000000000004';
ALTER TABLE public.actions ENABLE TRIGGER actions_enforce_rules;
UPDATE public.execution_grants SET expires_at = clock_timestamp() - interval '1 second'
 WHERE action_id = '9c000000-0000-0000-0000-000000000004';

SELECT pg_temp.expect_value('expired executing read action is reclaimed',
  $q$SELECT (public.claim_mock_action('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-2'::bytea)) IS NOT NULL)::text$q$,
  'true');
SELECT pg_temp.expect_value('reclaimed read action increments attempts to 2',
  $q$SELECT attempts::text FROM actions WHERE id='9c000000-0000-0000-0000-000000000004'$q$,
  '2');

SELECT pg_temp.expect_value('reclaimed read grant can be consumed',
  $q$SELECT (public.consume_mock_grant('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-2'::bytea)) ->> 'tool_key')$q$, 'order_identity_check');

SELECT pg_temp.expect_error('complete_mock_action refuses read actions without output',
  $q$SELECT public.complete_mock_action('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-2'::bytea), 'identity:NW-1001')$q$,
  'read actions require an output payload');

SELECT pg_temp.expect_error('complete_mock_read refuses identity_verified=true when order_found=false',
  $q$SELECT public.complete_mock_read('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-2'::bytea), 'identity:NW-1001',
             '{"order_found":false,"identity_verified":true}'::jsonb)$q$,
  'verified identity requires a found order');

SELECT pg_temp.expect_value('complete_mock_read records output',
  $q$SELECT public.complete_mock_read('9c000000-0000-0000-0000-000000000004', 'w2',
             sha256('grant-ic-2'::bytea), 'identity:NW-1001',
             '{"order_found":true,"identity_verified":true}'::jsonb)::text$q$,
  'true');
SELECT pg_temp.expect_value('complete_mock_read verifies case identity',
  $q$SELECT identity_verified::text FROM cases WHERE id='9c000000-0000-0000-0000-000000000001'$q$,
  'true');

SELECT pg_temp.expect_error('verified NW-1001 cannot authorize order_lookup for NW-1002 on same message',
  $q$INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                          risk_level, input, status)
     VALUES ('10000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000001',
             '9c000000-0000-0000-0000-000000000002', gen_random_uuid(), 'wrong-order',
             'order_lookup', 'read', '{"order_number":"NW-1002"}', 'proposed')$q$,
  'requires verified identity for the same message and order');

SELECT pg_temp.expect_error('verified NW-1001 on msg 1 cannot authorize order_lookup on msg 2',
  $q$INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                          risk_level, input, status)
     VALUES ('10000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000001',
             '9c000000-0000-0000-0000-000000000003', gen_random_uuid(), 'wrong-msg',
             'order_lookup', 'read', '{"order_number":"NW-1001"}', 'proposed')$q$,
  'requires verified identity for the same message and order');

SELECT pg_temp.expect_ok('verified NW-1001 on msg 1 authorizes order_lookup for NW-1001 on msg 1',
  $q$INSERT INTO actions (id, tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                          risk_level, input, status)
     VALUES ('9c000000-0000-0000-0000-000000000005', '10000000-0000-0000-0000-000000000001',
             '9c000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000002',
             gen_random_uuid(), 'lookup-msg-1-order', 'order_lookup', 'read',
             '{"order_number":"NW-1001"}', 'proposed')$q$);

SELECT pg_temp.expect_value('order_lookup on msg 1 can be claimed while identity is verified',
  $q$SELECT (public.claim_mock_action('9c000000-0000-0000-0000-000000000005', 'w1',
             sha256('grant-ol-1'::bytea)) IS NOT NULL)::text$q$, 'true');

-- Now msg 2 runs an identity check for NW-1002 that fails: it must clear case identity
-- and block any in-flight order_lookup grant consumption.
INSERT INTO actions (id, tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                     risk_level, input, status)
VALUES ('9c000000-0000-0000-0000-000000000006', '10000000-0000-0000-0000-000000000001',
        '9c000000-0000-0000-0000-000000000001', '9c000000-0000-0000-0000-000000000003',
        gen_random_uuid(), 'id-check-msg-2', 'order_identity_check', 'read',
        '{"order_number":"NW-1002","sender_email":"emma.jones@example.com"}', 'proposed');

SELECT public.claim_mock_action('9c000000-0000-0000-0000-000000000006', 'w1', sha256('grant-ic-msg2'::bytea)) \g /dev/null
SELECT public.consume_mock_grant('9c000000-0000-0000-0000-000000000006', 'w1', sha256('grant-ic-msg2'::bytea)) \g /dev/null
SELECT pg_temp.expect_value('failed identity check completes',
  $q$SELECT public.complete_mock_read('9c000000-0000-0000-0000-000000000006', 'w1',
             sha256('grant-ic-msg2'::bytea), 'identity:NW-1002',
             '{"order_found":true,"identity_verified":false}'::jsonb)::text$q$, 'true');
SELECT pg_temp.expect_value('failed identity check clears case identity_verified and invalidates prior order check',
  $q$SELECT (NOT (SELECT identity_verified FROM cases WHERE id='9c000000-0000-0000-0000-000000000001')
             AND NOT public.order_identity_is_verified(
               '10000000-0000-0000-0000-000000000001',
               '9c000000-0000-0000-0000-000000000001',
               '9c000000-0000-0000-0000-000000000002',
               'NW-1001'))::text$q$, 'true');

SELECT pg_temp.expect_value('in-flight order_lookup grant cannot be consumed after failed check',
  $q$SELECT (public.consume_mock_grant('9c000000-0000-0000-0000-000000000005', 'w1',
             sha256('grant-ol-1'::bytea)) IS NULL)::text$q$, 'true');
