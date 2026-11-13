INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
VALUES ('98000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', gen_random_uuid(), 'core-approval',
        'invoice_schedule_payment', 'money', 'pending_approval');

INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, required_role,
                               reason, status, sla_due_at, resolved_at)
VALUES ('98000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', 'action', '98000000-0000-0000-0000-000000000001',
        'agent', 'Fixture approval', 'approved', 'infinity', now());

SELECT pg_temp.expect_value('request snapshots trusted role, status and SLA',
  $q$SELECT (required_role = 'finance' AND status = 'pending' AND resolved_at IS NULL
             AND sla_due_at = created_at + make_interval(mins => s.approval_sla_minutes))::text
       FROM approval_requests r JOIN tenant_settings s USING (tenant_id)
      WHERE r.id = '98000000-0000-0000-0000-000000000002'$q$, 'true');
SELECT pg_temp.expect_error('core cannot rewrite approval authority',
  $q$UPDATE approval_requests SET required_role='agent', status='approved'$q$, 'permission denied');
SELECT pg_temp.expect_error('core cannot directly insert approval decisions',
  $q$INSERT INTO approval_decisions (tenant_id, request_id, reviewer_id, decision)
     VALUES ('10000000-0000-0000-0000-000000000001','98000000-0000-0000-0000-000000000002',
             '20000000-0000-0000-0000-000000000003','approved')$q$, 'permission denied');
SELECT pg_temp.expect_error('core cannot rewrite decisions',
  $q$UPDATE approval_decisions SET decision='approved'$q$, 'permission denied');
SELECT pg_temp.expect_ok('core may update Slack metadata',
  $q$UPDATE approval_requests SET slack_channel='fixture' WHERE id='98000000-0000-0000-0000-000000000002'$q$);
SELECT pg_temp.expect_value('watchdog cannot expire a request before the SLA',
  $q$SELECT public.expire_approval_request('98000000-0000-0000-0000-000000000002')::text$q$, 'false');
SELECT pg_temp.expect_ok('core records a role-checked approval through the transition',
  $q$SELECT public.record_approval_decision('98000000-0000-0000-0000-000000000002',
      '20000000-0000-0000-0000-000000000003','approved','api')$q$);
SELECT pg_temp.expect_value('decision and request share the database timestamp',
  $q$SELECT (d.decided_at = r.resolved_at AND d.decided_at >= r.created_at
             AND d.decided_at < r.sla_due_at)::text
       FROM approval_requests r JOIN approval_decisions d ON d.request_id = r.id
      WHERE r.id = '98000000-0000-0000-0000-000000000002'$q$, 'true');

INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, amount, status)
VALUES ('98000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', gen_random_uuid(), 'replacement',
        'invoice_schedule_payment', 'money', 100, 'pending_approval');
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, reason)
VALUES ('98000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', 'action', '98000000-0000-0000-0000-000000000003', 'Fixture');

SELECT pg_temp.expect_error('an edit cannot replace tool input or destination',
  $q$SELECT public.record_approval_decision('98000000-0000-0000-0000-000000000004',
      '20000000-0000-0000-0000-000000000003','edited','api','{"input":{}}')$q$, 'valid amount or currency');
SELECT pg_temp.expect_ok('an edited action creates a replacement',
  $q$SELECT public.record_approval_decision('98000000-0000-0000-0000-000000000004',
      '20000000-0000-0000-0000-000000000003','edited','api','{"amount":120}')$q$);
SELECT pg_temp.expect_value('edit retires the original and binds a fresh finance request',
  $q$SELECT count(*)::text FROM approval_requests r JOIN actions a ON a.id = r.subject_id
      WHERE r.reason = 'Replacement following edited' AND r.status = 'pending'
        AND r.required_role = 'finance' AND r.payload_hash = a.payload_hash
        AND a.status = 'pending_approval' AND a.amount = 120 AND EXISTS (
          SELECT 1 FROM actions old_a WHERE old_a.id='98000000-0000-0000-0000-000000000003'
            AND old_a.status='cancelled')$q$, '1');
SELECT pg_temp.expect_ok('escalation opens a fresh admin request',
  $q$SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000003','escalated','api')
       FROM approval_requests r WHERE r.reason='Replacement following edited' AND r.status='pending'$q$);
SELECT pg_temp.expect_value('escalated successor requires an admin and remains pending',
  $q$SELECT count(*)::text FROM approval_requests r JOIN actions a ON a.id = r.subject_id
      WHERE r.reason = 'Replacement following escalated' AND r.status='pending'
        AND r.required_role='admin' AND r.payload_hash=a.payload_hash AND a.status='pending_approval'$q$, '1');

INSERT INTO policy_decisions (id, tenant_id, case_id, pack_id, trace_id, decision, rule_id,
                              rule_source, reason, policy_version, required_role)
SELECT '98000000-0000-0000-0000-000000000005', tenant_id, id, pack_id, gen_random_uuid(),
       'needs_approval', 'fixture', 'pack', 'Review draft', '1', 'finance'
  FROM cases WHERE id = '92000000-0000-0000-0000-000000000001';
INSERT INTO drafts (id, tenant_id, case_id, policy_decision_id, channel, recipient, body)
VALUES ('98000000-0000-0000-0000-000000000006', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', '98000000-0000-0000-0000-000000000005',
        'email', 'fixture@example.test', 'Original');
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, reason)
VALUES ('98000000-0000-0000-0000-000000000007', '10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001', 'draft', '98000000-0000-0000-0000-000000000006', 'Fixture draft');

SELECT pg_temp.expect_error('pending draft content freezes when review is requested',
  $q$UPDATE drafts SET body='Changed' WHERE id='98000000-0000-0000-0000-000000000006'$q$,
  'draft content is frozen');
SELECT pg_temp.expect_ok('draft edit opens a replacement review',
  $q$SELECT public.record_approval_decision('98000000-0000-0000-0000-000000000007',
      '20000000-0000-0000-0000-000000000003','edited','api','{"body":"Replacement"}')$q$);
SELECT pg_temp.expect_error('an edit is insufficient to approve its replacement draft',
  $q$UPDATE drafts SET status='approved' WHERE body='Replacement'$q$,
  'requires exact policy or human authorization');
SELECT pg_temp.expect_ok('finance approves the exact replacement draft',
  $q$SELECT public.record_approval_decision(r.id,'20000000-0000-0000-0000-000000000003','approved','api')
       FROM approval_requests r JOIN drafts d ON d.id = r.subject_id WHERE d.body='Replacement'$q$);
SELECT pg_temp.expect_ok('a valid human decision authorizes the replacement draft',
  $q$UPDATE drafts SET status='approved' WHERE body='Replacement'$q$);
