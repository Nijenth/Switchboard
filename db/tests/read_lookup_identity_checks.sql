-- Read tool results (ADR 0019, migration 020) and order-scoped identity / lookup recovery
-- (ADRs 0021–0023, migrations 021–023).

SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null

INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, endpoint_id)
SELECT '9c000000-0000-0000-0000-000000000001', e.tenant_id, e.tenant_pack_id, tp.pack_id, 'email', e.id
  FROM channel_endpoints e JOIN tenant_packs tp ON (tp.tenant_id, tp.id) = (e.tenant_id, e.tenant_pack_id)
 WHERE e.channel = 'email' AND e.address = 'support@northwind.example';

INSERT INTO messages (id, tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction,
                      channel, body_text, sender_address)
SELECT msg.id, c.tenant_id, c.id, c.endpoint_id, gen_random_uuid(), msg.key, 'inbound', 'email',
       msg.body, 'Emma.Jones@example.com'
  FROM cases c CROSS JOIN (VALUES
    ('9c000000-0000-0000-0000-000000000002'::uuid, 'lookup-msg-1', 'Where is NW-1001?'),
    ('9c000000-0000-0000-0000-000000000003'::uuid, 'lookup-msg-2', 'What about NW-1002?')
  ) msg(id, key, body)
 WHERE c.id = '9c000000-0000-0000-0000-000000000001';

SELECT pg_temp.expect_error('identity check cannot borrow another sender address',
  $q$INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                          risk_level, input, status)
     SELECT m.tenant_id, m.case_id, m.id, m.trace_id, 'forged-sender', 'order_identity_check',
            'read', '{"order_number":"NW-1001","sender_email":"different@example.test"}', 'proposed'
       FROM messages m WHERE m.id='9c000000-0000-0000-0000-000000000002'$q$,
  'requires the inbound message sender');

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

INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key,
                     risk_level, input, status)
SELECT m.tenant_id, m.case_id, m.id, m.trace_id, 'lookup-output-formats', 'order_lookup',
       'read', '{"order_number":"NW-1001"}', 'proposed'
  FROM messages m WHERE m.id='9c000000-0000-0000-0000-000000000002';

SELECT public.claim_mock_action(id, 'w1', sha256('format-test'::bytea))
  FROM actions WHERE idempotency_key='lookup-output-formats' \g /dev/null
SELECT public.consume_mock_grant(id, 'w1', sha256('format-test'::bytea))
  FROM actions WHERE idempotency_key='lookup-output-formats' \g /dev/null

SELECT pg_temp.expect_error('database rejects invalid ' || v.label,
  format($q$SELECT public.complete_mock_read(
    (SELECT id FROM actions WHERE idempotency_key='lookup-output-formats'), 'w1',
    sha256('format-test'::bytea), 'order:NW-1001', %L::jsonb)$q$,
    '{"order_status":"shipped","carrier":"FedEx","tracking_number":"771200000000004","estimated_delivery_at":null}'::jsonb
      || v.override),
  'invalid identity-gated tool result')
FROM (VALUES
  ('carrier format', '{"carrier":"FedEx 123"}'::jsonb),
  ('tracking format', '{"tracking_number":"abc123"}'),
  ('tracking type', '{"tracking_number":771200000000004}'),
  ('status enum', '{"order_status":"unrecognized"}'),
  ('delivery date', '{"estimated_delivery_at":"2026-02-30T15:30:00Z"}'),
  ('delivery timezone', '{"estimated_delivery_at":"2026-12-01T15:30:00"}'),
  ('unexpected field', '{"customer_email":"sample@example.test"}')
) v(label, override);

SELECT pg_temp.expect_value('format rejection leaves the original grant usable for valid output',
  $q$SELECT public.complete_mock_read(
    (SELECT id FROM actions WHERE idempotency_key='lookup-output-formats'), 'w1',
    sha256('format-test'::bytea), 'order:NW-1001',
    '{"order_status":"shipped","carrier":"ExampleCarrier","tracking_number":"771200000000004",
      "estimated_delivery_at":"2026-12-01T15:30:00.125+05:30"}')::text$q$, 'true');

-- Verify the registry's nullability and required-field contracts as well.
SELECT pg_temp.expect_value('registry allows all-null result for a missing order',
  $q$SELECT public.valid_tool_output(
    (SELECT output_schema FROM tools WHERE key='order_lookup'),
    '{"order_status":null,"carrier":null,"tracking_number":null,"estimated_delivery_at":null}')::text$q$, 'true');
SELECT pg_temp.expect_value('registry requires every declared required field',
  $q$SELECT public.valid_tool_output(
    (SELECT output_schema FROM tools WHERE key='order_lookup'),
    '{"order_status":null,"carrier":null,"tracking_number":null}')::text$q$, 'false');

-- Test tools are installed only in this rolled-back safety check.
INSERT INTO tools (key, name, description, risk_level, requires_verified_identity, input_schema, output_schema)
SELECT 'fixture_sender_check', 'Fixture', 'Alternate identity check', 'read', false, input_schema, output_schema
  FROM tools WHERE key='order_identity_check'
UNION ALL
SELECT 'fixture_partial_check', 'Fixture', 'Identity output without sender input requirement', 'read', false,
       '{"required":["order_number"]}'::jsonb, output_schema
  FROM tools WHERE key='order_identity_check'
UNION ALL
SELECT 'fixture_scoped_read', 'Fixture', 'Alternate order-scoped read', 'read', true, input_schema,
       '{"type":"object","properties":{"receipt":{"type":"string","pattern":"^R-[0-9]+$"}},
         "required":["receipt"],"additionalProperties":false}'::jsonb
  FROM tools WHERE key='order_lookup'
UNION ALL
SELECT 'fixture_unscoped_read', 'Fixture', 'Schema has no order field', 'read', true,
       '{"type":"object"}'::jsonb, output_schema
  FROM tools WHERE key='order_lookup';

INSERT INTO pack_tools (pack_id, tool_key)
SELECT c.pack_id, t.key FROM cases c CROSS JOIN tools t
 WHERE c.id='9c000000-0000-0000-0000-000000000001'
   AND (t.key LIKE 'fixture_%' OR t.key IN ('refund_eligibility_check','refund_issue'));

SELECT pg_temp.expect_error('an alternate identity check rejects a mismatched sender',
  $q$INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
     SELECT tenant_id,case_id,message_id,trace_id,'fixture-bad-sender','fixture_sender_check',
            'read',input || '{"sender_email":"other@example.test"}','proposed'
       FROM actions WHERE idempotency_key='id-check-msg-1'$q$,
  'requires the inbound message sender');

INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
SELECT tenant_id,case_id,message_id,trace_id,t.key,t.key,'read', input ||
       jsonb_build_object('order_number', CASE WHEN t.key='fixture_sender_check'
                                               THEN 'NW-1009' ELSE 'NW-1010' END), 'proposed'
  FROM actions CROSS JOIN tools t
 WHERE idempotency_key='id-check-msg-1' AND t.key IN ('fixture_sender_check','fixture_partial_check');

SELECT pg_temp.expect_value('a read with the identity output alone cannot establish identity',
  $q$SELECT public.is_identity_check_tool(t)::text FROM tools t WHERE key='fixture_partial_check'$q$, 'false');
SELECT public.claim_mock_action(id,'w1',sha256('partial-id'::bytea))
  FROM actions WHERE idempotency_key='fixture_partial_check' \g /dev/null
SELECT public.consume_mock_grant(id,'w1',sha256('partial-id'::bytea))
  FROM actions WHERE idempotency_key='fixture_partial_check' \g /dev/null
SELECT pg_temp.expect_value('an identity-shaped result can complete without granting identity',
  $q$SELECT public.complete_mock_read(id,'w1',sha256('partial-id'::bytea),'match',
            '{"order_found":true,"identity_verified":true}')::text
       FROM actions WHERE idempotency_key='fixture_partial_check'$q$, 'true');
SELECT pg_temp.expect_value('a partial registry match cannot verify its order on a verified case',
  $q$SELECT public.order_identity_is_verified(a)::text FROM actions a
       WHERE idempotency_key='fixture_partial_check'$q$, 'false');

SELECT pg_temp.expect_value('the alternate identity check can be claimed',
  $q$SELECT (public.claim_mock_action(id,'w1',sha256('alternate-id'::bytea)) IS NOT NULL)::text
       FROM actions WHERE idempotency_key='fixture_sender_check'$q$, 'true');
SELECT pg_temp.expect_value('the alternate identity check grant can be consumed',
  $q$SELECT public.consume_mock_grant(id,'w1',sha256('alternate-id'::bytea))->>'tool_key'
       FROM actions WHERE idempotency_key='fixture_sender_check'$q$, 'fixture_sender_check');
SELECT pg_temp.expect_value('the alternate identity check can complete and verify its order',
  $q$SELECT (public.complete_mock_read(id,'w1',sha256('alternate-id'::bytea),'match',
            '{"order_found":true,"identity_verified":true}'))::text
       FROM actions WHERE idempotency_key='fixture_sender_check'$q$, 'true');

INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
SELECT tenant_id,case_id,message_id,trace_id,'alternate-result','fixture_scoped_read','read',
       '{"order_number":"NW-1009"}','proposed'
  FROM actions WHERE idempotency_key='fixture_sender_check';

SELECT pg_temp.expect_value('the alternate order-scoped tool claims against verified identity',
  $q$SELECT (public.claim_mock_action(id,'w1',sha256('alternate-result'::bytea)) IS NOT NULL)::text
       FROM actions WHERE idempotency_key='alternate-result'$q$, 'true');
SELECT pg_temp.expect_value('the alternate order-scoped tool dispatches against verified identity',
  $q$SELECT public.consume_mock_grant(id,'w1',sha256('alternate-result'::bytea))->>'tool_key'
       FROM actions WHERE idempotency_key='alternate-result'$q$, 'fixture_scoped_read');
SELECT pg_temp.expect_error('the alternate tool rejects output that violates its own schema',
  $q$SELECT public.complete_mock_read(id,'w1',sha256('alternate-result'::bytea),'result','{"receipt":"X-1"}')
       FROM actions WHERE idempotency_key='alternate-result'$q$, 'invalid identity-gated tool result');
SELECT pg_temp.expect_value('the alternate tool accepts output matching its own schema',
  $q$SELECT public.complete_mock_read(id,'w1',sha256('alternate-result'::bytea),'result','{"receipt":"R-1"}')::text
       FROM actions WHERE idempotency_key='alternate-result'$q$, 'true');

SELECT pg_temp.expect_value('unsupported output schema keywords invalidate the result',
  $q$SELECT public.valid_tool_output(output_schema || '{"oneOf":[]}', '{"receipt":"R-1"}')::text
       FROM tools WHERE key='fixture_scoped_read'$q$, 'false');
SELECT pg_temp.expect_value('an optional order field does not make an identity-gated tool order scoped',
  $q$SELECT public.is_order_scoped_tool(jsonb_populate_record(t, '{"input_schema":{
        "type":"object","properties":{"order_number":{"type":"string"}}}}'))::text
       FROM tools t WHERE key='fixture_unscoped_read'$q$, 'false');

SELECT pg_temp.expect_error('an unscoped identity-gated tool cannot be inserted past proposed',
  $q$INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
     SELECT tenant_id,case_id,message_id,trace_id,'unscoped-pending','fixture_unscoped_read','read',
            '{"order_number":"NW-1001"}','pending_approval'
       FROM actions WHERE idempotency_key='id-check-msg-1'$q$,
  'must require order_number in its input schema');

INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status,amount,currency)
SELECT m.tenant_id,m.case_id,m.id,m.trace_id,'unscoped:' || t.key || ':' || p.input::text,t.key,t.risk_level,
       p.input,'proposed',CASE WHEN t.risk_level='money' THEN 10 END,
       CASE WHEN t.risk_level='money' THEN 'USD' END
  FROM messages m CROSS JOIN tools t
 CROSS JOIN (VALUES ('{"order_number":"NW-1001"}'::jsonb), ('{"order_number":"NW-1002"}'), ('{}')) p(input)
 WHERE m.id='9c000000-0000-0000-0000-000000000002'
   AND t.key IN ('fixture_unscoped_read','refund_eligibility_check','refund_issue');

SELECT pg_temp.expect_error('an unscoped identity-gated proposal cannot advance to approval',
  $q$UPDATE actions SET status='pending_approval' WHERE idempotency_key LIKE 'unscoped:fixture_unscoped_read:%'$q$,
  'must require order_number in its input schema');

-- Model the pre-023 refund approval state in the disposable transaction.
ALTER TABLE public.actions DISABLE TRIGGER actions_validate_order;
ALTER TABLE public.actions DISABLE TRIGGER actions_validate_refund_evidence;
ALTER TABLE public.approval_requests DISABLE TRIGGER approval_requests_validate_order_money;
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,required_role,reason)
SELECT tenant_id,case_id,'action',id,'manager','Synthetic approval'
  FROM actions WHERE idempotency_key LIKE 'unscoped:refund_issue:%';
UPDATE actions SET status='pending_approval' WHERE idempotency_key LIKE 'unscoped:refund_issue:%';
SELECT public.record_approval_decision(id,'20000000-0000-0000-0000-000000000002','approved','api')
  FROM approval_requests WHERE subject_id IN (SELECT id FROM actions WHERE idempotency_key LIKE 'unscoped:refund_issue:%')
  \g /dev/null
UPDATE actions SET status='approved',approved_by='20000000-0000-0000-0000-000000000002'
 WHERE idempotency_key LIKE 'unscoped:refund_issue:%';
ALTER TABLE public.approval_requests ENABLE TRIGGER approval_requests_validate_order_money;
ALTER TABLE public.actions ENABLE TRIGGER actions_validate_order;
ALTER TABLE public.actions ENABLE TRIGGER actions_validate_refund_evidence;

SELECT pg_temp.expect_error(tool_key || ' claim refused with ' || input::text,
  format('SELECT public.claim_mock_action(%L,%L,sha256(%L::bytea))',id,'w1',idempotency_key),
  'must require order_number in its input schema')
FROM actions WHERE idempotency_key LIKE 'unscoped:%';

-- Only grants that 022 would have issued: an order that passed 022's refund
-- scope check and a read with no order input that 022 gated by the case flag alone.
ALTER TABLE public.actions DISABLE TRIGGER actions_validate_order;
ALTER TABLE public.actions DISABLE TRIGGER actions_validate_refund_evidence;
SELECT pg_temp.expect_value(tool_key || ' legacy grant exists',
  format('SELECT (public.claim_mock_action(%L,%L,sha256(%L::bytea)) IS NOT NULL)::text',id,'w1',idempotency_key),
  'true')
FROM actions WHERE idempotency_key LIKE 'unscoped:%'
  AND ((tool_key LIKE 'refund_%' AND input->>'order_number'='NW-1001')
       OR (tool_key='fixture_unscoped_read' AND input='{}'));
ALTER TABLE public.actions ENABLE TRIGGER actions_validate_order;
ALTER TABLE public.actions ENABLE TRIGGER actions_validate_refund_evidence;

SELECT pg_temp.expect_value(tool_key || ' cannot consume a legacy unscoped grant',
  format('SELECT (public.consume_mock_grant(%L,%L,sha256(%L::bytea)) IS NULL)::text',
         id,'w1',idempotency_key),'true')
FROM actions WHERE idempotency_key LIKE 'unscoped:%' AND status='executing';

-- Create the stale revision condition that otherwise allows a replacement for a read.
UPDATE execution_grants SET policy_revision=policy_revision-1 WHERE action_id IN (
  SELECT id FROM actions WHERE idempotency_key LIKE 'unscoped:fixture_unscoped_read:%' AND status='executing'
);
SELECT pg_temp.expect_value('an unscoped read cannot replace a legacy grant',
  $q$SELECT (public.replace_unused_mock_grant(id,'w1',sha256('alternate-grant'::bytea)) IS NULL)::text
       FROM actions WHERE idempotency_key LIKE 'unscoped:fixture_unscoped_read:%' AND status='executing'$q$, 'true');

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
