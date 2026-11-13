SELECT pg_temp.expect_error('draft status alone cannot grant outbound authorization',
  $q$UPDATE drafts SET status='approved' WHERE id='95000000-0000-0000-0000-000000000001'$q$,
  'requires exact policy or human authorization');

INSERT INTO policy_decisions (id, tenant_id, case_id, pack_id, trace_id, decision, rule_id,
                              rule_source, reason, policy_version, allowed_tools, draft_id, draft_payload_hash)
SELECT '99000000-0000-0000-0000-000000000001', d.tenant_id, d.case_id, c.pack_id, gen_random_uuid(),
       'auto', 'fixture', 'pack', 'Fixture allow', '1', ARRAY['send_reply'], d.id, d.payload_hash
  FROM drafts d JOIN cases c ON (c.tenant_id,c.id) = (d.tenant_id,d.case_id)
 WHERE d.id='95000000-0000-0000-0000-000000000001';
UPDATE drafts SET policy_decision_id='99000000-0000-0000-0000-000000000001'
 WHERE id='95000000-0000-0000-0000-000000000001';

INSERT INTO actions (id, tenant_id, case_id, policy_decision_id, trace_id, idempotency_key,
                     tool_key, risk_level, input, status)
SELECT fixture.id, d.tenant_id, d.case_id, d.policy_decision_id, gen_random_uuid(), fixture.key,
       'send_reply', 'external_comm', jsonb_build_object('draft_id', d.id), 'proposed'
  FROM drafts d CROSS JOIN (VALUES
    ('99000000-0000-0000-0000-000000000002'::uuid, 'authorized-draft-1'),
    ('99000000-0000-0000-0000-000000000003'::uuid, 'authorized-draft-2')
  ) fixture(id,key)
 WHERE d.id='95000000-0000-0000-0000-000000000001';

SELECT pg_temp.expect_error('policy allow alone cannot dispatch an unapproved draft',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='99000000-0000-0000-0000-000000000002'$q$, 'requires input.draft_id of an approved draft');
SELECT pg_temp.expect_ok('exact policy binding authorizes the draft',
  $q$UPDATE drafts SET status='approved' WHERE id='95000000-0000-0000-0000-000000000001'$q$);
SELECT pg_temp.expect_ok('current policy and exact draft permit a claim',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='99000000-0000-0000-0000-000000000002'$q$);
SELECT pg_temp.expect_error('two actions cannot claim the same outbound draft',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='99000000-0000-0000-0000-000000000003'$q$, 'actions_one_execution_per_draft');

INSERT INTO policy_decisions (tenant_id, case_id, pack_id, trace_id, decision, rule_id, rule_source,
                              reason, policy_version)
SELECT tenant_id, id, pack_id, gen_random_uuid(), 'escalate', 'fixture', 'global', 'Escalate', '1'
  FROM cases WHERE id='92000000-0000-0000-0000-000000000001';
SELECT pg_temp.expect_error('a newer escalation supersedes the automatic allow',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='99000000-0000-0000-0000-000000000003'$q$, 'requires a current policy allow');

INSERT INTO actions (id, tenant_id, case_id, trace_id, idempotency_key, tool_key, risk_level, status)
VALUES ('99000000-0000-0000-0000-000000000004','10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001',gen_random_uuid(),'read-exempt','po_lookup','read','proposed'),
       ('99000000-0000-0000-0000-000000000005','10000000-0000-0000-0000-000000000001',
        '92000000-0000-0000-0000-000000000001',gen_random_uuid(),'approval-required','po_lookup','read','pending_approval');
SELECT pg_temp.expect_ok('a scoped read can be claimed without a policy decision',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='99000000-0000-0000-0000-000000000004'$q$);
SELECT pg_temp.expect_error('pending approval requires a real request even for a non-money tool',
  $q$UPDATE actions SET status='approved'
     WHERE id='99000000-0000-0000-0000-000000000005'$q$, 'has no approval request');
SELECT pg_temp.expect_error('a proposed action cannot switch adapters',
  $q$UPDATE actions SET adapter='real' WHERE id='99000000-0000-0000-0000-000000000005'$q$,
  'destination and policy binding are immutable');
