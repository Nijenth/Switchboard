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
