-- =====================================================================
-- db/tests/safety_checks.sql — executable database safety tests
--
-- Run against a freshly migrated + seeded database, as the DB owner/superuser:
--   psql -X -v ON_ERROR_STOP=1 -d <db> -f db/tests/safety_checks.sql
-- Prints PASS/FAIL per check. Exits non-zero if any check fails (use in CI).
-- Everything runs inside one transaction and is ROLLED BACK: the database is left unchanged.
--
-- What these prove: database-level invariants (constraints, triggers, RLS, privileges).
-- What they do NOT prove: application security, real integration safety, end-to-end reliability.
-- =====================================================================
\set ON_ERROR_STOP 1
\pset footer off
\pset tuples_only on
SET client_min_messages = notice;
BEGIN;

SELECT set_config('test.pass', '0', false), set_config('test.fail', '0', false) \g /dev/null

CREATE FUNCTION pg_temp.record(ok boolean, label text, detail text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF ok THEN
    PERFORM set_config('test.pass', (current_setting('test.pass')::int + 1)::text, false);
    RAISE NOTICE 'PASS  %', label;
  ELSE
    PERFORM set_config('test.fail', (current_setting('test.fail')::int + 1)::text, false);
    RAISE NOTICE 'FAIL  %  -- %', label, detail;
  END IF;
END $$;

-- Statement must fail with an error containing `pattern`.
CREATE FUNCTION pg_temp.expect_error(label text, stmt text, pattern text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE stmt;
  EXCEPTION WHEN OTHERS THEN
    PERFORM pg_temp.record(SQLERRM ILIKE '%' || pattern || '%', label, 'wrong error: ' || SQLERRM);
    RETURN;
  END;
  PERFORM pg_temp.record(false, label, 'no error raised');
END $$;

-- Statement must succeed.
CREATE FUNCTION pg_temp.expect_ok(label text, stmt text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE stmt;
  PERFORM pg_temp.record(true, label);
EXCEPTION WHEN OTHERS THEN
  PERFORM pg_temp.record(false, label, SQLERRM);
END $$;

-- Query must return exactly `expected` (as text).
CREATE FUNCTION pg_temp.expect_value(label text, query text, expected text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE got text;
BEGIN
  EXECUTE query INTO got;
  PERFORM pg_temp.record(got IS NOT DISTINCT FROM expected, label, format('expected %s, got %s', expected, got));
EXCEPTION WHEN OTHERS THEN
  PERFORM pg_temp.record(false, label, SQLERRM);
END $$;

-- ---------------------------------------------------------------------
-- Fixtures (as owner). Northwind = 10000000-…-0001 from the seed.
-- A second tenant "Acme" proves isolation.
-- ---------------------------------------------------------------------
INSERT INTO tenants (id, slug, name) VALUES ('91000000-0000-0000-0000-000000000001', 'acme-test', 'Acme Test Co');
INSERT INTO tenant_packs (id, tenant_id, pack_key, pack_id) VALUES
  ('91000000-0000-0000-0000-000000000002', '91000000-0000-0000-0000-000000000001', 'ecommerce_support', '30000000-0000-0000-0000-000000000001');
INSERT INTO staff_users (id, tenant_id, email, name, role) VALUES
  ('91000000-0000-0000-0000-000000000003', '91000000-0000-0000-0000-000000000001', 'admin@acme.example', 'Acme Admin', 'admin');
INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, external_thread_id) VALUES
  ('91000000-0000-0000-0000-000000000004', '91000000-0000-0000-0000-000000000001', '91000000-0000-0000-0000-000000000002',
   '30000000-0000-0000-0000-000000000001', 'email', 'acme-th-1');

-- Northwind invoice case + message
INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, external_thread_id) VALUES
  ('92000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000003',
   '30000000-0000-0000-0000-000000000003', 'email', 'nw-th-1');
INSERT INTO messages (id, tenant_id, case_id, trace_id, idempotency_key, direction, channel, body_text) VALUES
  ('92000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', '92000000-0000-0000-0000-000000000001',
   gen_random_uuid(), 'email:test-1', 'inbound', 'email', 'Invoice attached');

\echo
\echo '== 1. Data integrity & tenant consistency =='
SELECT pg_temp.expect_error('duplicate webhook is rejected (idempotency)',
  $q$INSERT INTO messages (tenant_id, case_id, trace_id, idempotency_key, direction, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001',gen_random_uuid(),'email:test-1','inbound','email')$q$,
  'messages_tenant_id_idempotency_key_key');

SELECT pg_temp.expect_error('message cannot point at another tenant''s case',
  $q$INSERT INTO messages (tenant_id, case_id, trace_id, idempotency_key, direction, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004',gen_random_uuid(),'email:x','inbound','email')$q$,
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
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','approved')$q$,
  'cannot satisfy required role');

SELECT pg_temp.expect_error('manager cannot approve a finance request',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002','approved')$q$,
  'cannot satisfy required role');

SELECT pg_temp.expect_error('another tenant''s admin cannot approve',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000003','approved')$q$,
  'not found in this tenant');

SELECT pg_temp.expect_ok('finance can approve',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000003','approved')$q$);

SELECT pg_temp.expect_value('decision resolves the request automatically',
  $q$SELECT status FROM approval_requests WHERE id='94000000-0000-0000-0000-000000000001'$q$, 'approved');

SELECT pg_temp.expect_error('resolved request cannot be decided again',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000004','rejected')$q$,
  'already approved');

SELECT pg_temp.expect_error('approved_by must be the actual approving reviewer',
  $q$UPDATE actions SET status='approved', approved_by='20000000-0000-0000-0000-000000000004' WHERE id='93000000-0000-0000-0000-000000000001'$q$,
  'must be the reviewer who approved');

SELECT pg_temp.expect_ok('action becomes approved with the right reviewer',
  $q$UPDATE actions SET status='approved', approved_by='20000000-0000-0000-0000-000000000003' WHERE id='93000000-0000-0000-0000-000000000001'$q$);

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
UPDATE actions SET amount = 100000 WHERE id = '93000000-0000-0000-0000-000000000002';   -- swapped after request

SELECT pg_temp.expect_error('approval is refused when the payload changed after the request',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','94000000-0000-0000-0000-000000000002','20000000-0000-0000-0000-000000000003','approved')$q$,
  'changed after approval was requested');

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
  'requires input.draft_id of an approved draft');

SELECT pg_temp.expect_error('only one live draft per inbound message',
  $q$INSERT INTO drafts (tenant_id, case_id, in_reply_to_message_id, channel, recipient, body)
     VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000002','email','x@y.z','dup')$q$,
  'drafts_one_live_idx');

UPDATE drafts SET status = 'approved' WHERE id = '95000000-0000-0000-0000-000000000001';
SELECT pg_temp.expect_error('approved draft text is frozen',
  $q$UPDATE drafts SET body='Also, please send your bank details.' WHERE id='95000000-0000-0000-0000-000000000001'$q$,
  'content is frozen');

\echo
\echo '== 6. Row-level security & least privilege (as app roles) =='
SET ROLE app_core;
SELECT set_config('app.tenant_id', '', false) \g /dev/null
SELECT pg_temp.expect_value('no tenant context → sees zero cases (fail closed)',
  $q$SELECT count(*)::text FROM cases$q$, '0');

SELECT pg_temp.expect_value('ingest can resolve tenant from inbound address without context',
  $q$SELECT tenant_id::text FROM resolve_channel_endpoint('email','invoices@northwind.example')$q$,
  '10000000-0000-0000-0000-000000000001');

SELECT set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', false) \g /dev/null
SELECT pg_temp.expect_value('Northwind context cannot see Acme''s case',
  $q$SELECT count(*)::text FROM cases WHERE id='91000000-0000-0000-0000-000000000004'$q$, '0');
SELECT pg_temp.expect_value('Northwind context sees its own case',
  $q$SELECT count(*)::text FROM cases WHERE id='92000000-0000-0000-0000-000000000001'$q$, '1');
SELECT pg_temp.expect_value('Northwind context sees no Acme staff',
  $q$SELECT count(*)::text FROM staff_users WHERE tenant_id='91000000-0000-0000-0000-000000000001'$q$, '0');

SELECT pg_temp.expect_error('cannot write a row for another tenant',
  $q$INSERT INTO messages (tenant_id, case_id, trace_id, idempotency_key, direction, channel)
     VALUES ('91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004',gen_random_uuid(),'email:rls','inbound','email')$q$,
  'row-level security');

SELECT pg_temp.expect_error('app cannot change which tools a pack may use',
  $q$UPDATE pack_tools SET approval_role = NULL WHERE tool_key = 'invoice_schedule_payment'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot change a tool''s risk level',
  $q$UPDATE tools SET risk_level = 'read' WHERE key = 'refund_issue'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot change tenant pack config',
  $q$UPDATE tenant_packs SET config = '{}'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot edit audit records',
  $q$UPDATE audit_log SET actor_id = 'x'$q$, 'permission denied');
SELECT pg_temp.expect_error('app cannot delete operational data',
  $q$DELETE FROM cases WHERE id='92000000-0000-0000-0000-000000000001'$q$, 'permission denied');

SELECT pg_temp.expect_ok('app can append to the audit log',
  $q$INSERT INTO audit_log (tenant_id, actor_type, actor_id, event_type)
     VALUES ('10000000-0000-0000-0000-000000000001','system','test','test.event')$q$);
RESET ROLE;

SET ROLE app_n8n_mock;
SELECT pg_temp.expect_error('n8n mock role cannot read engine tables',
  $q$SELECT count(*) FROM cases$q$, 'permission denied');
SELECT pg_temp.expect_value('n8n mock role can read the mock shop',
  $q$SELECT count(*)::text FROM demo.shop_orders WHERE order_number='NW-1001'$q$, '1');
RESET ROLE;

SET ROLE app_reporting;
SELECT pg_temp.expect_error('reporting role cannot read raw messages',
  $q$SELECT count(*) FROM messages$q$, 'permission denied');
SELECT pg_temp.expect_ok('reporting role can read KPI views',
  $q$SELECT count(*) FROM v_daily_kpis$q$);
RESET ROLE;

\echo
\echo '== 7. Audit log =='
SELECT pg_temp.expect_error('audit UPDATE blocked (even for owner)',   $q$UPDATE audit_log SET actor_id='x'$q$, 'append-only');
SELECT pg_temp.expect_error('audit DELETE blocked (even for owner)',   $q$DELETE FROM audit_log$q$,           'append-only');
SELECT pg_temp.expect_error('audit TRUNCATE blocked (even for owner)', $q$TRUNCATE audit_log$q$,              'append-only');
SELECT pg_temp.expect_value('hash chain is intact',
  $q$SELECT left(verify_audit_chain(), 2)$q$, 'OK');

-- Simulate a superuser who disables the trigger and edits history: must be DETECTED.
ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;
UPDATE audit_log SET details = '{"tampered":true}' WHERE chain_seq = 1;
ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;
SELECT pg_temp.expect_value('superuser tampering is detected by the hash chain',
  $q$SELECT verify_audit_chain()$q$, 'BROKEN: row 1 was modified');

-- ---------------------------------------------------------------------
\echo
SELECT current_setting('test.pass') AS passed, current_setting('test.fail') AS failed \gset
\echo 'Passed: ' :passed '   Failed: ' :failed
DO $$ BEGIN
  IF current_setting('test.fail')::int > 0 THEN
    RAISE EXCEPTION '% safety check(s) failed', current_setting('test.fail');
  END IF;
END $$;
ROLLBACK;
