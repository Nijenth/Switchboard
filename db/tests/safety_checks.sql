-- =====================================================================
-- db/tests/safety_checks.sql — executable database safety tests
--
-- Run against a freshly migrated + seeded database, as the schema owner:
--   psql -X -v ON_ERROR_STOP=1 -d <db> -f db/tests/safety_checks.sql
-- Prints PASS/FAIL per check. Exits non-zero if any check fails (use in CI).
-- Test rows roll back. Sequences can advance, so run only on a disposable cluster.
--
-- What these prove: database-level invariants (constraints, triggers, RLS, privileges).
-- What they do NOT prove: application security, real integration safety, end-to-end reliability.
-- =====================================================================
\ir check_helpers.sql
BEGIN;
\ir check_fixtures.sql

SELECT pg_temp.expect_value('schema owner owns cases and lacks superuser, role creation, and RLS bypass',
  $q$SELECT (c.relowner = r.oid AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolbypassrls)::text
       FROM pg_catalog.pg_class c CROSS JOIN pg_catalog.pg_roles r
      WHERE c.oid = 'public.cases'::regclass AND r.rolname = session_user$q$, 'true');

SELECT pg_temp.expect_value('timeline projects only its approved columns',
  $q$SELECT array_agg(column_name::text ORDER BY ordinal_position)::text
       FROM information_schema.columns
      WHERE table_schema='public' AND table_name='v_case_timeline'$q$,
  '{case_id,tenant_id,created_at,event_kind,intent,confidence,flags,tool_key,status,result_reason,rule_id,rule_source,decision,risk_level,amount,currency,required_role,audit_event,audit_sequence}');
SELECT pg_temp.expect_value('only the reporting application role may select the timeline',
  $q$SELECT (has_table_privilege('app_reporting','public.v_case_timeline','SELECT')
        AND NOT has_table_privilege('app_core','public.v_case_timeline','SELECT')
        AND NOT has_table_privilege('app_config_loader','public.v_case_timeline','SELECT')
        AND NOT has_table_privilege('app_n8n_mock','public.v_case_timeline','SELECT'))::text$q$, 'true');
SELECT pg_temp.expect_value('reporting has no readable columns on raw timeline sources',
  $q$SELECT bool_and(NOT has_any_column_privilege('app_reporting','public.' || source,'SELECT'))::text
       FROM unnest(ARRAY['cases','messages','classifications','actions','policy_decisions',
         'drafts','approval_requests','approval_decisions','audit_log','extractions']) source$q$, 'true');

\echo
\echo '== 1. Data integrity & tenant consistency =='
SELECT pg_temp.expect_error('duplicate webhook is rejected (idempotency)',
  $q$INSERT INTO messages (tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction, channel)
     SELECT tenant_id, case_id, endpoint_id, gen_random_uuid(), idempotency_key, direction, channel
       FROM messages WHERE id='92000000-0000-0000-0000-000000000002'$q$,
  'messages_source_idempotency_key');

SELECT pg_temp.expect_error('message cannot point at another tenant''s case',
  $q$INSERT INTO messages (tenant_id, case_id, trace_id, idempotency_key, direction, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004',gen_random_uuid(),'email:x','outbound','email')$q$,
  'messages_case_tenant_fk');

SELECT pg_temp.expect_error('intent must belong to the case''s pack',
  $q$INSERT INTO classifications (tenant_id, case_id, pack_id, intent_key, method, priority, confidence)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000003','order_status','llm','normal',0.9)$q$,
  'classifications_pack_id_intent_key_fkey');

SELECT pg_temp.expect_error('extraction must have a source',
  $q$INSERT INTO extractions (tenant_id, case_id, pack_id, schema_key, schema_version, data)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000003','invoice',1,'{}')$q$,
  'extractions_check');

SELECT pg_temp.expect_error('tenant pack must reference a matching pack key',
  $q$INSERT INTO tenant_packs (tenant_id, pack_key, pack_id)
     VALUES ('91000000-0000-0000-0000-000000000001','sales_leads','30000000-0000-0000-0000-000000000001')$q$,
  'tenant_packs_pack_id_pack_key_fkey');

\ir case_scope_checks.sql
\ir endpoint_scope_checks.sql

\echo
\echo '== 2. Tool permissions =='
SELECT pg_temp.expect_error('unregistered tool is rejected',
  $q$INSERT INTO actions (tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',gen_random_uuid(),'t-1','wire_money_now','money','proposed')$q$,
  'not registered');

SELECT pg_temp.expect_error('risk level cannot be downgraded vs registry',
  $q$INSERT INTO actions (tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',gen_random_uuid(),'t-2','invoice_schedule_payment','read','proposed')$q$,
  'does not match registry');

SELECT pg_temp.expect_error('tool not in the pack is rejected (order_lookup on invoice case)',
  $q$INSERT INTO actions (tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',gen_random_uuid(),'t-3','order_lookup','read','proposed')$q$,
  'not allowed for this case''s pack');

SELECT pg_temp.expect_error('action cannot be created already executed',
  $q$INSERT INTO actions (tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',gen_random_uuid(),'t-4','po_lookup','read','executed')$q$,
  'must be created as proposed');

\echo
\echo '== 3. Money actions & approval binding =='
SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true) \g /dev/null
INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, amount, currency, input, status) VALUES
  ('93000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   gen_random_uuid(), 'pay-1', 'invoice_schedule_payment', 'money', 4800, 'USD', '{"bill":"LT-5521","vendor":"Loom & Thread"}', 'pending_approval');

SELECT pg_temp.expect_error('money action cannot be approved without an approval request',
  $q$UPDATE actions SET status='approved', approved_by='20000000-0000-0000-0000-000000000003' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'has no approval request');

INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, required_role, reason, sla_due_at) VALUES
  ('94000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   'action', '93000000-0000-0000-0000-000000000001', 'finance', 'Payment', now() + interval '30 minutes');

SELECT pg_temp.expect_value('approval request captured the payload hash',
  $q$SELECT (r.payload_hash = a.payload_hash)::text FROM approval_requests r JOIN actions a ON a.id = r.subject_id
     WHERE r.id='94000000-0000-0000-0000-000000000001'$q$, 'true');

SELECT pg_temp.expect_error('approval subject must be in the same tenant',
  $q$INSERT INTO approval_requests (tenant_id, case_id, subject_type, subject_id, required_role, reason, sla_due_at)
     VALUES ('91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004','action','93000000-0000-0000-0000-000000000001','admin','x',now())$q$,
  'not found in this tenant/case');

SELECT pg_temp.expect_error('second pending request for the same action is rejected',
  $q$INSERT INTO approval_requests (tenant_id, case_id, subject_type, subject_id, required_role, reason, sla_due_at)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','action','93000000-0000-0000-0000-000000000001','finance','dup',now())$q$,
  'approval_requests_one_pending_idx');

SELECT pg_temp.expect_error('agent cannot approve a finance request',
  $q$SELECT public.record_approval_decision('94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','approved','api')$q$,
  'cannot satisfy required role');

SELECT pg_temp.expect_error('manager cannot approve a finance request',
  $q$SELECT public.record_approval_decision('94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002','approved','api')$q$,
  'cannot satisfy required role');

SELECT pg_temp.expect_error('another tenant''s admin cannot approve',
  $q$SELECT public.record_approval_decision('94000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000003','approved','api')$q$,
  'not found in this tenant');

SELECT pg_temp.expect_ok('finance can approve',
  $q$SELECT public.record_approval_decision('94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000003','approved','api')$q$);

SELECT pg_temp.expect_value('decision resolves the request automatically',
  $q$SELECT status FROM approval_requests WHERE id='94000000-0000-0000-0000-000000000001'$q$, 'approved');

SELECT pg_temp.expect_error('resolved request cannot be decided again',
  $q$SELECT public.record_approval_decision('94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000004','rejected','api')$q$,
  'already approved');

SELECT pg_temp.expect_error('the recorded reviewer cannot be changed after promotion',
  $q$UPDATE actions SET status='approved', approved_by='20000000-0000-0000-0000-000000000004' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'action authority cannot change');

SELECT pg_temp.expect_value('recording approval promotes the action with the right reviewer',
  $q$SELECT (status='approved' AND approved_by='20000000-0000-0000-0000-000000000003')::text
      FROM actions WHERE id='93000000-0000-0000-0000-000000000001'$q$, 'true');

SELECT pg_temp.expect_error('approved payload is frozen (amount change blocked)',
  $q$UPDATE actions SET amount = 48000 WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'payload is frozen');

-- Approval given for one payload cannot be used for another.
INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, amount, currency, input, status) VALUES
  ('93000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   gen_random_uuid(), 'pay-2', 'invoice_schedule_payment', 'money', 100, 'USD', '{"bill":"X-1"}', 'proposed');
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, required_role, reason, sla_due_at) VALUES
  ('94000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   'action', '93000000-0000-0000-0000-000000000002', 'finance', 'Payment', now() + interval '30 minutes');
SELECT pg_temp.expect_error('request freezes a proposed action payload',
  $q$UPDATE actions SET amount = 100000 WHERE id = '93000000-0000-0000-0000-000000000002'$q$,
  'payload is frozen');

SAVEPOINT write_action_expiry;
INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, input, status) VALUES
  ('93000000-0000-0000-0000-000000000009', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   gen_random_uuid(), 'post-expire', 'invoice_post_to_accounting', 'write', '{"bill":"X-2"}', 'pending_approval');
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, reason) VALUES
  ('94000000-0000-0000-0000-000000000009', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   'action', '93000000-0000-0000-0000-000000000009', 'Post invoice');
UPDATE approval_requests SET sla_due_at = clock_timestamp() - interval '1 second'
 WHERE id = '94000000-0000-0000-0000-000000000009';
SELECT pg_temp.expect_value('expiring an action approval returns true',
  $q$SELECT public.expire_approval_request('94000000-0000-0000-0000-000000000009')::text$q$, 'true');
SELECT pg_temp.expect_value('expiring an action approval cancels the action and logs audit entries',
  $q$SELECT (
       (SELECT status FROM approval_requests WHERE id='94000000-0000-0000-0000-000000000009') = 'expired'
       AND (SELECT status FROM actions WHERE id='93000000-0000-0000-0000-000000000009') = 'cancelled'
       AND EXISTS (SELECT 1 FROM audit_log WHERE entity_type='approval_request' AND entity_id='94000000-0000-0000-0000-000000000009' AND event_type='approval.expired')
       AND EXISTS (
         SELECT 1 FROM audit_log l JOIN actions a ON a.id='93000000-0000-0000-0000-000000000009'
          WHERE l.entity_type='action' AND l.entity_id=a.id AND l.event_type='action.cancelled'
            AND l.trace_id=a.trace_id AND l.details->>'reason'='approval_expired'
            AND l.details->>'approval_request_id'='94000000-0000-0000-0000-000000000009'
       )
     )::text$q$, 'true');
ROLLBACK TO SAVEPOINT write_action_expiry;

\echo
\echo '== 4. Execution: claims, concurrency, state machine =='
SELECT pg_temp.expect_error('executing requires a claim (worker + lease)',
  $q$UPDATE actions SET status='executing' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'requires claimed_by');

SELECT pg_temp.expect_value('first worker wins the claim',
  $q$WITH c AS (UPDATE actions SET status='executing', claimed_by='worker-A', lease_expires_at=now()+interval '2 minutes'
               WHERE id='93000000-0000-0000-0000-000000000001' AND status='approved' RETURNING 1) SELECT count(*)::text FROM c$q$, '1');

SELECT pg_temp.expect_value('second worker gets nothing (no double execution)',
  $q$WITH c AS (UPDATE actions SET status='executing', claimed_by='worker-B', lease_expires_at=now()+interval '2 minutes'
               WHERE id='93000000-0000-0000-0000-000000000001' AND status='approved' RETURNING 1) SELECT count(*)::text FROM c$q$, '0');

SELECT pg_temp.expect_error('illegal transition executing -> approved',
  $q$UPDATE actions SET status='approved' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'illegal action transition');

SELECT pg_temp.expect_ok('crash mid-call is recorded as unknown_outcome',
  $q$UPDATE actions SET status='unknown_outcome' WHERE id='93000000-0000-0000-0000-000000000001'$q$);

SELECT pg_temp.expect_error('unknown_outcome cannot be blindly retried (must reconcile first)',
  $q$UPDATE actions SET status='executing', claimed_by='worker-C', lease_expires_at=now()+interval '2 minutes' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'illegal action transition');

SELECT pg_temp.expect_ok('reconciler confirms it executed',
  $q$UPDATE actions SET status='executed', external_ref='QB-BILLPAY-881', executed_at=now() WHERE id='93000000-0000-0000-0000-000000000001'$q$);

SELECT pg_temp.expect_error('executed action is final',
  $q$UPDATE actions SET status='failed' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'is final');

\echo
\echo '== 5. Outbound messages =='
INSERT INTO drafts (id, tenant_id, case_id, in_reply_to_message_id, channel, recipient, body) VALUES
  ('95000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   '92000000-0000-0000-0000-000000000002', 'email', 'billing@loomandthread.example', 'Thanks, received.');
INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, input, status) VALUES
  ('93000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   gen_random_uuid(), 'send-1', 'send_reply', 'external_comm', '{"draft_id":"95000000-0000-0000-0000-000000000001"}', 'proposed');

SELECT pg_temp.expect_error('cannot send a draft that is not approved',
  $q$UPDATE actions SET status='executing', claimed_by='w', lease_expires_at=now()+interval '1 minute' WHERE id='93000000-0000-0000-0000-000000000003'$q$,
  'requires a current policy allow');

SELECT pg_temp.expect_error('only one live draft per inbound message',
  $q$INSERT INTO drafts (tenant_id, case_id, in_reply_to_message_id, channel, recipient, body)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000002','email','x@y.z','dup')$q$,
  'drafts_one_live_idx');

\ir execution_authorization_checks.sql
SELECT pg_temp.expect_error('approved draft text is frozen',
  $q$UPDATE drafts SET body='Also, please send your bank details.' WHERE id='95000000-0000-0000-0000-000000000001'$q$,
  'content is frozen');

\ir approval_freshness_checks.sql
\ir single_reply_review_checks.sql
\ir read_lookup_identity_checks.sql
\ir order_refund_checks.sql

\echo
\echo '== 7. Audit log =='
SELECT pg_temp.expect_error('audit UPDATE blocked (even for owner)',   $q$UPDATE audit_log SET actor_id='x'$q$, 'append-only');
SELECT pg_temp.expect_error('audit DELETE blocked (even for owner)',   $q$DELETE FROM audit_log$q$,           'append-only');
SELECT pg_temp.expect_error('audit TRUNCATE blocked (even for owner)', $q$TRUNCATE audit_log$q$,              'append-only');
SELECT pg_temp.expect_value('hash chain is intact',
  $q$SELECT left(verify_audit_chain(), 2)$q$, 'OK');

-- Simulate a schema owner who disables the trigger and edits history: must be DETECTED.
ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;
UPDATE audit_log SET details = '{"tampered":true}' WHERE chain_seq = 1;
ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;
SELECT pg_temp.expect_value('schema owner tampering is detected by the hash chain',
  $q$SELECT verify_audit_chain()$q$, 'BROKEN: row 1 was modified');

\ir check_results.sql
ROLLBACK;
