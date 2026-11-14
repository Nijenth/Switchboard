INSERT INTO policy_decisions (id, tenant_id, case_id, pack_id, trace_id, decision, rule_id,
                              rule_source, reason, policy_version, required_role)
SELECT '9a000000-0000-0000-0000-000000000001', tenant_id, id, pack_id, gen_random_uuid(),
       'needs_approval', 'fixture', 'pack', 'Human review', '1', 'finance'
  FROM cases WHERE id='92000000-0000-0000-0000-000000000001';

INSERT INTO actions (id, tenant_id, case_id, policy_decision_id, trace_id, idempotency_key,
                     tool_key, risk_level, status)
SELECT '9a000000-0000-0000-0000-000000000002', tenant_id, case_id, id, gen_random_uuid(),
       'stale-human-approval', 'invoice_schedule_payment', 'money', 'pending_approval'
  FROM policy_decisions WHERE id='9a000000-0000-0000-0000-000000000001';
INSERT INTO drafts (id, tenant_id, case_id, policy_decision_id, channel, recipient, body)
SELECT fixture.id, tenant_id, case_id, policy_decisions.id, 'email', 'fixture@example.test', 'Fixture'
  FROM policy_decisions CROSS JOIN (VALUES
    ('9a000000-0000-0000-0000-000000000003'::uuid),
    ('9a000000-0000-0000-0000-000000000004'::uuid)
  ) fixture(id) WHERE policy_decisions.id='9a000000-0000-0000-0000-000000000001';
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, reason)
SELECT fixture.id, tenant_id, case_id, fixture.kind, fixture.subject, 'Fixture'
  FROM policy_decisions CROSS JOIN (VALUES
    ('9a000000-0000-0000-0000-000000000005'::uuid, 'action', '9a000000-0000-0000-0000-000000000002'::uuid),
    ('9a000000-0000-0000-0000-000000000006'::uuid, 'draft', '9a000000-0000-0000-0000-000000000003'::uuid),
    ('9a000000-0000-0000-0000-000000000007'::uuid, 'draft', '9a000000-0000-0000-0000-000000000004'::uuid)
  ) fixture(id, kind, subject) WHERE policy_decisions.id='9a000000-0000-0000-0000-000000000001';
SELECT public.record_approval_decision(id, '20000000-0000-0000-0000-000000000003', 'approved', 'api')
  FROM approval_requests WHERE id IN ('9a000000-0000-0000-0000-000000000005',
                                      '9a000000-0000-0000-0000-000000000007') \g /dev/null

SELECT pg_temp.expect_error('a draft cannot be marked sent before its send action completes',
  $q$UPDATE drafts SET status='sent' WHERE id='95000000-0000-0000-0000-000000000001'$q$,
  'sent draft requires an executed send action');
UPDATE actions SET status='executed' WHERE id='99000000-0000-0000-0000-000000000002';
SELECT pg_temp.expect_ok('an executed send action allows its draft to become sent',
  $q$UPDATE drafts SET status='sent' WHERE id='95000000-0000-0000-0000-000000000001'$q$);

INSERT INTO policy_decisions (tenant_id, case_id, pack_id, trace_id, decision, rule_id,
                              rule_source, reason, policy_version)
SELECT tenant_id, id, pack_id, gen_random_uuid(), 'escalate', 'fixture', 'global', 'Escalate', '1'
  FROM cases WHERE id='92000000-0000-0000-0000-000000000001';
SELECT pg_temp.expect_error('an escalation invalidates a human-approved action claim',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='9a000000-0000-0000-0000-000000000002'$q$, 'stricter policy decision requires fresh approval');
SELECT pg_temp.expect_error('an escalation prevents approval on the earlier draft request',
  $q$SELECT public.record_approval_decision('9a000000-0000-0000-0000-000000000006',
    '20000000-0000-0000-0000-000000000003','approved','api')$q$,
  'requires exact policy or human authorization');
SELECT pg_temp.expect_value('an already approved draft loses authority after escalation',
  $q$SELECT public.draft_is_authorized(d, c.policy_revision)::text FROM drafts d
      JOIN cases c ON (c.tenant_id, c.id) = (d.tenant_id, d.case_id)
     WHERE d.id='9a000000-0000-0000-0000-000000000004'$q$, 'false');

UPDATE approval_requests SET sla_due_at=clock_timestamp()
 WHERE id='9a000000-0000-0000-0000-000000000006';
SELECT public.expire_approval_request('9a000000-0000-0000-0000-000000000006') \g /dev/null
INSERT INTO approval_requests (id, tenant_id, case_id, subject_type, subject_id, reason)
SELECT '9a000000-0000-0000-0000-000000000008', tenant_id, case_id, 'draft', id, 'Fresh review'
  FROM drafts WHERE id='9a000000-0000-0000-0000-000000000003';
SELECT pg_temp.expect_error('fresh review after escalation requires the stricter role',
  $q$SELECT public.record_approval_decision('9a000000-0000-0000-0000-000000000008',
    '20000000-0000-0000-0000-000000000003','approved','api')$q$, 'cannot satisfy required role admin');
SELECT public.record_approval_decision('9a000000-0000-0000-0000-000000000008',
  '20000000-0000-0000-0000-000000000004','approved','api') \g /dev/null
SELECT pg_temp.expect_value('fresh authorized review promotes the draft after escalation',
  $q$SELECT status FROM drafts WHERE id='9a000000-0000-0000-0000-000000000003'$q$, 'approved');
