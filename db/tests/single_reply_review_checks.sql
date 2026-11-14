-- One Slack review per reply (ADR 0018, migration 019). A human-approved, current draft
-- authorizes only its canonical mock send action; anything else needs its own authority.

-- Builds a fresh support case with an inbound message, a pending draft bound to a
-- needs_approval decision, a draft review request, and a proposed send action.
CREATE FUNCTION pg_temp.single_review_fixture(p_case uuid, p_message uuid, p_draft uuid,
                                              p_decision uuid, p_action uuid, p_key text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  tenant constant uuid := '10000000-0000-0000-0000-000000000001';
BEGIN
  INSERT INTO cases (id, tenant_id, tenant_pack_id, pack_id, channel, endpoint_id)
  SELECT p_case, e.tenant_id, e.tenant_pack_id, tp.pack_id, 'email', e.id
    FROM channel_endpoints e JOIN tenant_packs tp ON (tp.tenant_id, tp.id) = (e.tenant_id, e.tenant_pack_id)
   WHERE e.channel = 'email' AND e.address = 'support@northwind.example';
  INSERT INTO messages (id, tenant_id, case_id, endpoint_id, trace_id, idempotency_key, direction,
                        channel, body_text)
  SELECT p_message, tenant, p_case, c.endpoint_id, gen_random_uuid(), 'single-review:' || p_case,
         'inbound', 'email', 'Where is my parcel?'
    FROM cases c WHERE c.id = p_case;
  INSERT INTO drafts (id, tenant_id, case_id, in_reply_to_message_id, channel, recipient, body)
  VALUES (p_draft, tenant, p_case, p_message, 'email', 'customer@example.test', 'Thanks for writing.');
  INSERT INTO policy_decisions (id, tenant_id, case_id, message_id, pack_id, trace_id, decision,
                                rule_id, rule_source, reason, policy_version, draft_id, draft_payload_hash)
  SELECT p_decision, tenant, p_case, p_message, c.pack_id, gen_random_uuid(), 'needs_approval',
         'FIXTURE_REVIEW', 'pack', 'Review the reply', '1', d.id, d.payload_hash
    FROM drafts d JOIN cases c ON (c.tenant_id, c.id) = (d.tenant_id, d.case_id)
   WHERE d.id = p_draft;
  UPDATE drafts SET policy_decision_id = p_decision WHERE id = p_draft;
  INSERT INTO approval_requests (tenant_id, case_id, subject_type, subject_id, reason)
  VALUES (tenant, p_case, 'draft', p_draft, 'Review outbound reply');
  INSERT INTO actions (id, tenant_id, case_id, message_id, policy_decision_id, trace_id,
                       idempotency_key, tool_key, risk_level, input, status)
  VALUES (p_action, tenant, p_case, p_message, p_decision, gen_random_uuid(), p_key,
          'send_reply', 'external_comm', jsonb_build_object('draft_id', p_draft), 'proposed');
END $$;

-- Approves the draft review the way the signed Slack callback does.
CREATE FUNCTION pg_temp.approve_draft_review(p_draft uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  request uuid;
  reviewer uuid;
BEGIN
  PERFORM set_config('app.tenant_id', '10000000-0000-0000-0000-000000000001', true);
  SELECT id INTO STRICT request FROM approval_requests
   WHERE subject_type = 'draft' AND subject_id = p_draft AND status = 'pending';
  SELECT s.id INTO STRICT reviewer FROM staff_users s JOIN approval_requests r ON r.id = request
   WHERE s.tenant_id = r.tenant_id AND s.active
     AND (s.role IN ('admin', r.required_role)
          OR (r.required_role = 'agent' AND s.role IN ('manager', 'finance')))
   ORDER BY s.role = 'admin', s.id LIMIT 1;
  PERFORM record_approval_decision(request, reviewer, 'approved', 'slack');
  PERFORM set_config('app.tenant_id', '', true);
END $$;

SELECT pg_temp.single_review_fixture(
  '9a000000-0000-0000-0000-00000000000a', '9a000000-0000-0000-0000-000000000001',
  '9a000000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000003',
  '9a000000-0000-0000-0000-000000000004', 'draft-send:9a000000-0000-0000-0000-000000000002');

SELECT pg_temp.expect_error('single review: an unreviewed draft cannot authorize its send',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='9a000000-0000-0000-0000-000000000004'$q$, 'requires a current policy allow');

SELECT pg_temp.approve_draft_review('9a000000-0000-0000-0000-000000000002');
SELECT pg_temp.expect_value('single review: approving the draft approves the draft only',
  $q$SELECT d.status || '/' || a.status FROM drafts d, actions a
      WHERE d.id='9a000000-0000-0000-0000-000000000002' AND a.id='9a000000-0000-0000-0000-000000000004'$q$,
  'approved/proposed');
SELECT pg_temp.expect_value('single review: no separate action approval request exists',
  $q$SELECT count(*)::text FROM approval_requests
      WHERE subject_type='action' AND subject_id='9a000000-0000-0000-0000-000000000004'$q$, '0');

INSERT INTO actions (id, tenant_id, case_id, message_id, policy_decision_id, trace_id,
                     idempotency_key, tool_key, risk_level, input, status)
VALUES ('9a000000-0000-0000-0000-000000000005', '10000000-0000-0000-0000-000000000001',
        '9a000000-0000-0000-0000-00000000000a', '9a000000-0000-0000-0000-000000000001',
        '9a000000-0000-0000-0000-000000000003', gen_random_uuid(), 'not-the-canonical-send',
        'send_reply', 'external_comm',
        jsonb_build_object('draft_id', '9a000000-0000-0000-0000-000000000002'::uuid), 'proposed');
SELECT pg_temp.expect_error('single review: a non-canonical send cannot borrow the draft review',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='9a000000-0000-0000-0000-000000000005'$q$, 'requires a current policy allow');

SELECT pg_temp.expect_ok('single review: the reviewed draft authorizes its canonical send',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='9a000000-0000-0000-0000-000000000004'$q$);

SELECT pg_temp.single_review_fixture(
  '9b000000-0000-0000-0000-00000000000b', '9b000000-0000-0000-0000-000000000001',
  '9b000000-0000-0000-0000-000000000002', '9b000000-0000-0000-0000-000000000003',
  '9b000000-0000-0000-0000-000000000004', 'draft-send:9b000000-0000-0000-0000-000000000002');
SELECT pg_temp.approve_draft_review('9b000000-0000-0000-0000-000000000002');
INSERT INTO policy_decisions (tenant_id, case_id, message_id, pack_id, trace_id, decision, rule_id,
                              rule_source, reason, policy_version)
SELECT tenant_id, id, '9b000000-0000-0000-0000-000000000001', pack_id, gen_random_uuid(),
       'escalate', 'G1', 'global', 'Later safety flag', '1'
  FROM cases WHERE id='9b000000-0000-0000-0000-00000000000b';
SELECT pg_temp.expect_error('single review: a newer escalation voids the draft review',
  $q$UPDATE actions SET status='executing', claimed_by='fixture', lease_expires_at=now()+interval '1 minute'
     WHERE id='9b000000-0000-0000-0000-000000000004'$q$, 'requires a current policy allow');
