-- Order money authority

-- Reuse the order identity fixture and establish fresh evidence.
CREATE FUNCTION pg_temp.complete_fixture_eligibility(label text, result jsonb) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  action_id uuid;
  digest bytea := sha256(convert_to(label,'UTF8'));
BEGIN
  INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
  SELECT a.tenant_id,a.case_id,a.message_id,a.trace_id,label,'order_refund_eligibility','read',
    a.input - 'sender_email','proposed'
    FROM actions a WHERE a.idempotency_key='refund-fresh-identity'
  RETURNING id INTO action_id;
  PERFORM public.claim_mock_action(action_id,'fixture',digest);
  PERFORM public.consume_mock_grant(action_id,'fixture',digest);
  RETURN public.complete_mock_read(action_id,'fixture',digest,label,result);
END $$;

-- The preceding fixture ends with a failed identity check. Verify a fresh message.
INSERT INTO messages (id,tenant_id,case_id,endpoint_id,trace_id,idempotency_key,direction,channel,sender_address)
SELECT '9c000000-0000-0000-0000-000000000009',m.tenant_id,m.case_id,m.endpoint_id,gen_random_uuid(),
  'refund-fresh-message','inbound',m.channel,m.sender_address
  FROM messages m WHERE m.id='9c000000-0000-0000-0000-000000000002';
INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status)
SELECT m.tenant_id,m.case_id,m.id,m.trace_id,'refund-fresh-identity','fixture_sender_check','read',
  '{"order_number":"NW-1009","sender_email":"emma.jones@example.com"}','proposed'
  FROM messages m WHERE m.idempotency_key='refund-fresh-message';
SELECT public.claim_mock_action(id,'fixture',sha256('refund-identity'::bytea))
  FROM actions WHERE idempotency_key='refund-fresh-identity' \g /dev/null
SELECT public.consume_mock_grant(id,'fixture',sha256('refund-identity'::bytea))
  FROM actions WHERE idempotency_key='refund-fresh-identity' \g /dev/null
SELECT public.complete_mock_read(id,'fixture',sha256('refund-identity'::bytea),'matched',
  '{"order_found":true,"identity_verified":true}')
  FROM actions WHERE idempotency_key='refund-fresh-identity' \g /dev/null

CREATE FUNCTION pg_temp.propose_fixture_refund(label text, amount text, order_number text DEFAULT 'NW-1009')
RETURNS uuid LANGUAGE sql AS $$
  INSERT INTO actions (tenant_id,case_id,message_id,trace_id,idempotency_key,tool_key,risk_level,input,status,amount,currency)
  SELECT m.tenant_id,m.case_id,m.id,m.trace_id,label,'order_refund_issue','money',
    jsonb_build_object('order_number',order_number,'amount',amount,'currency','USD'),
    'pending_approval',amount::numeric,'USD'
    FROM messages m WHERE m.idempotency_key='refund-fresh-message'
  RETURNING id
$$;

SELECT pg_temp.expect_error('money action requires same-message eligibility evidence',
  $q$SELECT pg_temp.propose_fixture_refund('missing-evidence','25.00')$q$,
  'matching current eligibility evidence');

SELECT pg_temp.expect_value('eligible refund evidence completes',
  $q$SELECT pg_temp.complete_fixture_eligibility('refund-eligible',
      '{"eligible":true,"reason":"eligible","refundable_amount":"25.00","currency":"USD"}')::text$q$, 'true');
SAVEPOINT approval_evidence;
SELECT pg_temp.propose_fixture_refund('approval-evidence-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
SELECT tenant_id,case_id,'action',id,'Review fixture refund'
  FROM actions WHERE idempotency_key='approval-evidence-refund';
SELECT pg_temp.complete_fixture_eligibility('refund-revoked-before-approval',
    '{"eligible":false,"reason":"window_expired","refundable_amount":null,"currency":null}') \g /dev/null
SELECT pg_temp.expect_error('newer ineligibility stops approval via ' || channel,
  format($q$SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000002','approved',%L)
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='approval-evidence-refund'$q$,channel), 'matching current eligibility evidence')
  FROM (VALUES ('slack'),('api')) channels(channel);
ROLLBACK TO SAVEPOINT approval_evidence;
SAVEPOINT rejected_refund;
SELECT pg_temp.propose_fixture_refund('rejected-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
SELECT tenant_id,case_id,'action',id,'Review fixture refund'
  FROM actions WHERE idempotency_key='rejected-refund';
SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000002','rejected','slack')
  FROM approval_requests r JOIN actions a ON a.id=r.subject_id
 WHERE a.idempotency_key='rejected-refund' \g /dev/null
SELECT pg_temp.expect_value('a rejected Slack refund cannot claim execution',
  $q$SELECT (status='rejected' AND public.claim_mock_action(id,'fixture',sha256('rejected'::bytea)) IS NULL)::text
       FROM actions WHERE idempotency_key='rejected-refund'$q$, 'true');
ROLLBACK TO SAVEPOINT rejected_refund;
SAVEPOINT expired_refund;
UPDATE tenant_settings SET slack_workspace_id = 'T000REFUND'
 WHERE tenant_id = '10000000-0000-0000-0000-000000000001';
SELECT pg_temp.propose_fixture_refund('expired-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason,slack_channel,slack_message_ts)
SELECT tenant_id,case_id,'action',id,'Review fixture refund','C-DEMO-SUPPORT','10.123456'
  FROM actions WHERE idempotency_key='expired-refund';
SELECT pg_temp.expect_error('pending refund blocks parallel refund before expiry',
  $q$SELECT pg_temp.propose_fixture_refund('blocked-while-pending','25.00')$q$, 'actions_one_live_order_money');
UPDATE approval_requests SET sla_due_at = clock_timestamp() - interval '1 second'
 WHERE subject_type='action' AND subject_id=(SELECT id FROM actions WHERE idempotency_key='expired-refund');
SELECT pg_temp.expect_error('late Slack approval past SLA is refused before watchdog runs',
  $q$SELECT public.record_slack_approval(r.id,'T000REFUND','U-DEMO-MARCO','C-DEMO-SUPPORT','10.123456','approved')
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='expired-refund'$q$, 'already resolved or past its SLA');
SELECT pg_temp.expect_value('expire_approval_request expires pending refund approval',
  $q$SELECT public.expire_approval_request(r.id)::text
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='expired-refund'$q$, 'true');
SELECT pg_temp.expect_value('expiring refund approval cancels action and logs audit',
  $q$SELECT (
       r.status = 'expired'
       AND a.status = 'cancelled'
       AND EXISTS (SELECT 1 FROM audit_log WHERE entity_type='approval_request' AND entity_id=r.id AND event_type='approval.expired')
       AND EXISTS (
         SELECT 1 FROM audit_log l
          WHERE l.entity_type='action' AND l.entity_id=a.id AND l.event_type='action.cancelled'
            AND l.trace_id=a.trace_id AND l.details->>'reason'='approval_expired'
            AND l.details->>'approval_request_id'=r.id::text
       )
     )::text
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='expired-refund'$q$, 'true');
SELECT pg_temp.expect_error('late Slack approval after expiry is refused',
  $q$SELECT public.record_slack_approval(r.id,'T000REFUND','U-DEMO-MARCO','C-DEMO-SUPPORT','10.123456','approved')
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='expired-refund'$q$, 'already resolved or past its SLA');
SELECT pg_temp.expect_value('an expired refund can never be claimed or consumed',
  $q$SELECT (
       status='cancelled'
       AND public.claim_mock_action(id,'fixture',sha256('expired'::bytea)) IS NULL
       AND public.consume_mock_grant(id,'fixture',sha256('expired'::bytea)) IS NULL
     )::text
       FROM actions WHERE idempotency_key='expired-refund'$q$, 'true');
SELECT pg_temp.propose_fixture_refund('fresh-after-expiry','25.00') \g /dev/null
SELECT pg_temp.expect_value('expired refund releases order for a fresh refund proposal',
  $q$SELECT (status='pending_approval' AND amount=25.00)::text
       FROM actions WHERE idempotency_key='fresh-after-expiry'$q$, 'true');
ROLLBACK TO SAVEPOINT expired_refund;
SAVEPOINT edited_refund;
SELECT pg_temp.propose_fixture_refund('edited-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
SELECT tenant_id,case_id,'action',id,'Review fixture refund'
  FROM actions WHERE idempotency_key='edited-refund';
SELECT pg_temp.expect_error('edited refund amount is refused by eligibility check',
  $q$SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000002','edited','api','{"amount":10.00}')
       FROM approval_requests r JOIN actions a ON a.id=r.subject_id
      WHERE a.idempotency_key='edited-refund'$q$, 'matching current eligibility evidence');
SELECT pg_temp.expect_value('original refund stays pending after refused edit',
  $q$SELECT (a.status='pending_approval' AND a.amount=25.00 AND r.status='pending')::text
       FROM actions a JOIN approval_requests r ON r.subject_id=a.id
      WHERE a.idempotency_key='edited-refund'$q$, 'true');
ROLLBACK TO SAVEPOINT edited_refund;

SAVEPOINT approved_unclaimed_refund;
UPDATE tenant_settings SET approval_claim_window_minutes = 5
 WHERE tenant_id = '10000000-0000-0000-0000-000000000001';
SELECT pg_temp.propose_fixture_refund('approved-unclaimed-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
SELECT tenant_id,case_id,'action',id,'Review fixture refund'
  FROM actions WHERE idempotency_key='approved-unclaimed-refund';
-- A recorded approval without subject promotion models the legacy pending action shape.
SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000002','approved','api')
  FROM approval_requests r JOIN actions a ON a.id=r.subject_id
 WHERE a.idempotency_key='approved-unclaimed-refund' \g /dev/null
SELECT pg_temp.expect_value('approved request with unpromoted action is still reserved',
  $q$SELECT a.status FROM actions a JOIN approval_requests r ON r.subject_id=a.id
      WHERE a.idempotency_key='approved-unclaimed-refund' AND r.status='approved'$q$, 'pending_approval');
SELECT pg_temp.expect_value('unclaimed refund inside its claim window stays live',
  $q$SELECT public.cancel_unclaimed_approved_actions()::text$q$, '0');
UPDATE approval_requests SET resolved_at = clock_timestamp() - interval '6 minutes'
 WHERE subject_id=(SELECT id FROM actions WHERE idempotency_key='approved-unclaimed-refund');
SELECT set_config('app.tenant_id', '91000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT pg_temp.expect_value('another tenant cannot cancel the overdue refund',
  $q$SELECT public.cancel_unclaimed_approved_actions()::text$q$, '0');
SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null
SELECT pg_temp.expect_value('tenant claim window cancels the unpromoted refund',
  $q$SELECT public.cancel_unclaimed_approved_actions()::text$q$, '1');
SELECT pg_temp.expect_value('cancellation is idempotent',
  $q$SELECT public.cancel_unclaimed_approved_actions()::text$q$, '0');
SELECT pg_temp.expect_value('only core receives application execute permission',
  $q$SELECT (has_function_privilege('app_core','public.cancel_unclaimed_approved_actions()','EXECUTE')
        AND NOT has_function_privilege('app_n8n_mock','public.cancel_unclaimed_approved_actions()','EXECUTE')
        AND NOT has_function_privilege('app_config_loader','public.cancel_unclaimed_approved_actions()','EXECUTE')
        AND NOT has_function_privilege('app_reporting','public.cancel_unclaimed_approved_actions()','EXECUTE'))::text$q$, 'true');
SELECT pg_temp.expect_value('cancelled approved refund cannot be claimed or consumed',
  $q$SELECT (a.status='cancelled' AND r.status='approved'
        AND public.claim_mock_action(a.id,'fixture',sha256('unclaimed'::bytea)) IS NULL
        AND public.consume_mock_grant(a.id,'fixture',sha256('unclaimed'::bytea)) IS NULL)::text
      FROM actions a JOIN approval_requests r ON r.subject_id=a.id
     WHERE a.idempotency_key='approved-unclaimed-refund'$q$, 'true');
SELECT pg_temp.propose_fixture_refund('fresh-after-claim-window','25.00') \g /dev/null
ROLLBACK TO SAVEPOINT approved_unclaimed_refund;

SELECT pg_temp.expect_error('verified order cannot authorize a different order',
  $q$SELECT pg_temp.propose_fixture_refund('different-order','25.00','NW-1010')$q$,
  'requires verified identity for the same message and order');
SELECT pg_temp.expect_error('refund amount must exactly match executed evidence',
  $q$SELECT pg_temp.propose_fixture_refund('wrong-amount','25.01')$q$,
  'matching current eligibility evidence');

SELECT pg_temp.propose_fixture_refund('scoped-refund','25.00') \g /dev/null
INSERT INTO approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
SELECT tenant_id,case_id,'action',id,'Review fixture refund'
  FROM actions WHERE idempotency_key='scoped-refund';
SELECT pg_temp.expect_error('one live refund per tenant and order',
  $q$SELECT pg_temp.propose_fixture_refund('parallel-refund','25.00')$q$, 'actions_one_live_order_money');
SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000002','approved','api')
  FROM approval_requests r JOIN actions a ON a.id=r.subject_id WHERE a.idempotency_key='scoped-refund' \g /dev/null
UPDATE actions SET status='approved',approved_by='20000000-0000-0000-0000-000000000002'
 WHERE idempotency_key='scoped-refund';
SELECT pg_temp.expect_error('approved refund amount and currency are frozen',
  $q$UPDATE actions SET amount=99,currency='EUR' WHERE idempotency_key='scoped-refund'$q$,
  'payload is frozen');
SELECT pg_temp.expect_value('approved matching refund can be claimed',
  $q$SELECT (public.claim_mock_action(id,'fixture',sha256('refund-grant'::bytea)) IS NOT NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
UPDATE approval_requests SET resolved_at = clock_timestamp() - interval '61 minutes'
 WHERE subject_id=(SELECT id FROM actions WHERE idempotency_key='scoped-refund');
SELECT pg_temp.expect_value('watchdog leaves a claimed refund executing past the window',
  $q$SELECT public.cancel_unclaimed_approved_actions()::text$q$, '0');
SELECT pg_temp.expect_value('two workers cannot both claim the refund',
  $q$SELECT (public.claim_mock_action(id,'second',sha256('another-refund-grant'::bytea)) IS NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
SELECT pg_temp.expect_value('newer ineligible evidence completes',
  $q$SELECT pg_temp.complete_fixture_eligibility('refund-ineligible',
    '{"eligible":false,"reason":"window_expired","refundable_amount":null,"currency":null}')::text$q$, 'true');
SELECT pg_temp.expect_value('newer ineligibility stops consumption',
  $q$SELECT (public.consume_mock_grant(id,'fixture',sha256('refund-grant'::bytea)) IS NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
UPDATE execution_grants SET policy_revision=policy_revision-1
 WHERE action_id=(SELECT id FROM actions WHERE idempotency_key='scoped-refund');
SELECT pg_temp.expect_value('newer ineligibility also stops grant replacement',
  $q$SELECT (public.replace_unused_mock_grant(id,'fixture',sha256('replacement-refund'::bytea)) IS NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
SELECT pg_temp.expect_value('fresh matching approval evidence completes',
  $q$SELECT pg_temp.complete_fixture_eligibility('refund-eligible-again',
      '{"eligible":true,"reason":"eligible","refundable_amount":"25.00","currency":"USD"}')::text$q$, 'true');
SELECT pg_temp.expect_value('current matching evidence permits a replacement grant',
  $q$SELECT (public.replace_unused_mock_grant(id,'fixture',sha256('refund-grant'::bytea)) IS NOT NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
SELECT pg_temp.expect_value('approved refund can consume once with current eligibility',
  $q$SELECT (public.consume_mock_grant(id,'fixture',sha256('refund-grant'::bytea)) IS NOT NULL)::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
SELECT pg_temp.expect_error('refund completion requires output',
  $q$SELECT public.complete_mock_action(id,'fixture',sha256('refund-grant'::bytea),'receipt')
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'invalid identity-gated tool result');
SELECT pg_temp.expect_error('refund completion refuses incorrect output',
  $q$SELECT public.complete_mock_money(id,'fixture',sha256('refund-grant'::bytea),'receipt','{"status":"refunded"}')
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'invalid identity-gated tool result');
SELECT pg_temp.expect_value('refund completes with its validated output',
  $q$SELECT public.complete_mock_money(id,'fixture',sha256('refund-grant'::bytea),
    'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee','{"refund_reference":"eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee","status":"refunded"}')::text
       FROM actions WHERE idempotency_key='scoped-refund'$q$, 'true');
SELECT pg_temp.expect_error('an executed refund permanently reserves its tenant order',
  $q$SELECT pg_temp.propose_fixture_refund('new-refund','25.00')$q$, 'actions_one_live_order_money');
