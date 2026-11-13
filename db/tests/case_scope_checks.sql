INSERT INTO cases (id, tenant_id, channel) VALUES
  ('92000000-0000-0000-0000-000000000003','10000000-0000-0000-0000-000000000001','email');
INSERT INTO messages (id, tenant_id, case_id, trace_id, idempotency_key, direction, channel) VALUES
  ('92000000-0000-0000-0000-000000000004','91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004',gen_random_uuid(),'acme-scope','inbound','email');
INSERT INTO documents (id, tenant_id, case_id, storage_bucket, storage_key, filename, mime_type, size_bytes, sha256) VALUES
  ('92000000-0000-0000-0000-000000000005','91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004','test','scope','scope.pdf','application/pdf',0,repeat('0',64));
INSERT INTO ai_runs (id, tenant_id, trace_id, step, provider, model, status) VALUES
  ('92000000-0000-0000-0000-000000000006','91000000-0000-0000-0000-000000000001',gen_random_uuid(),'classify','fixture','fixture','success');
INSERT INTO policy_decisions (id, tenant_id, case_id, pack_id, trace_id, decision, rule_id, rule_source, reason, policy_version) VALUES
  ('92000000-0000-0000-0000-000000000007','91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000004','30000000-0000-0000-0000-000000000001',gen_random_uuid(),'ignore','fixture','default','fixture','1');

SELECT pg_temp.expect_error('case insert rejects a mismatched pack version',
  $q$INSERT INTO cases (tenant_id, tenant_pack_id, pack_id, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','40000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000001','email')$q$,
  'case requires an enabled tenant pack');
SELECT pg_temp.expect_error('case insert rejects a disabled pack',
  $q$INSERT INTO cases (tenant_id, tenant_pack_id, pack_id, channel)
     VALUES ('91000000-0000-0000-0000-000000000001','91000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000001','email')$q$,
  'case requires an enabled tenant pack');
SELECT pg_temp.expect_error('first routing rejects a mismatched pack version',
  $q$UPDATE cases SET tenant_pack_id='40000000-0000-0000-0000-000000000003', pack_id='30000000-0000-0000-0000-000000000001'
     WHERE id='92000000-0000-0000-0000-000000000003'$q$, 'case requires an enabled tenant pack');
SELECT pg_temp.expect_ok('first routing accepts the current tenant pack',
  $q$UPDATE cases SET tenant_pack_id='40000000-0000-0000-0000-000000000003', pack_id='30000000-0000-0000-0000-000000000003'
     WHERE id='92000000-0000-0000-0000-000000000003'$q$);

INSERT INTO packs (id, key, version, name, definition, checksum) VALUES
  ('92000000-0000-0000-0000-000000000008','invoice_processing',2,'Invoice Processing','{}','fixture');
SELECT pg_temp.expect_ok('tenant pack upgrades while old cases still exist',
  $q$UPDATE tenant_packs SET pack_id='92000000-0000-0000-0000-000000000008'
     WHERE id='40000000-0000-0000-0000-000000000003'$q$);
SELECT pg_temp.expect_error('an existing case cannot be repinned after the upgrade',
  $q$UPDATE cases SET pack_id='92000000-0000-0000-0000-000000000008'
     WHERE id='92000000-0000-0000-0000-000000000003'$q$, 'case pack is already pinned');
SELECT pg_temp.expect_ok('an existing case can still progress after the upgrade',
  $q$UPDATE cases SET status='in_progress' WHERE id='92000000-0000-0000-0000-000000000003'$q$);
SELECT pg_temp.expect_ok('a new case pins the upgraded version',
  $q$INSERT INTO cases (tenant_id, tenant_pack_id, pack_id, channel)
     VALUES ('10000000-0000-0000-0000-000000000001','40000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000008','email')$q$);
UPDATE tenant_packs SET pack_id='30000000-0000-0000-0000-000000000003' WHERE id='40000000-0000-0000-0000-000000000003';

-- Each row exercises one newly constrained reference on otherwise valid data.
SELECT pg_temp.expect_error(label, statement, constraint_name) FROM (VALUES
  ('document message belongs to its case', $q$INSERT INTO documents (tenant_id, case_id, message_id, storage_bucket, storage_key, filename, mime_type, size_bytes, sha256)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','test','other','scope.pdf','application/pdf',0,repeat('0',64))$q$, 'documents_message_case_fk'),
  ('AI run message belongs to its tenant', $q$INSERT INTO ai_runs (tenant_id, message_id, trace_id, step, provider, model, status)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000004',gen_random_uuid(),'classify','fixture','fixture','success')$q$, 'ai_runs_message_tenant_fk'),
  ('AI run document belongs to its tenant', $q$INSERT INTO ai_runs (tenant_id, document_id, trace_id, step, provider, model, status)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000005',gen_random_uuid(),'ocr','fixture','fixture','success')$q$, 'ai_runs_document_tenant_fk'),
  ('classification belongs to the case pack', $q$INSERT INTO classifications (tenant_id, case_id, pack_id, intent_key, method, priority, confidence)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000001','order_status','rules','normal',1)$q$, 'classifications_case_pack_fk'),
  ('classification message belongs to its case', $q$INSERT INTO classifications (tenant_id, case_id, message_id, pack_id, intent_key, method, priority, confidence)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003','supplier_invoice','rules','normal',1)$q$, 'classifications_msg_case_fk'),
  ('classification AI run belongs to its tenant', $q$INSERT INTO classifications (tenant_id, case_id, ai_run_id, pack_id, intent_key, method, priority, confidence)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000006','30000000-0000-0000-0000-000000000003','supplier_invoice','llm','normal',1)$q$, 'classifications_ai_run_tenant_fk'),
  ('extraction belongs to the case pack', $q$INSERT INTO extractions (tenant_id, case_id, message_id, pack_id, schema_key, schema_version, data)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000001','fixture',1,'{}')$q$, 'extractions_case_pack_fk'),
  ('extraction message belongs to its case', $q$INSERT INTO extractions (tenant_id, case_id, message_id, pack_id, schema_key, schema_version, data)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003','fixture',1,'{}')$q$, 'extractions_msg_case_fk'),
  ('extraction document belongs to its case', $q$INSERT INTO extractions (tenant_id, case_id, document_id, pack_id, schema_key, schema_version, data)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000005','30000000-0000-0000-0000-000000000003','fixture',1,'{}')$q$, 'extractions_doc_case_fk'),
  ('extraction AI run belongs to its tenant', $q$INSERT INTO extractions (tenant_id, case_id, message_id, ai_run_id, pack_id, schema_key, schema_version, data)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000002','92000000-0000-0000-0000-000000000006','30000000-0000-0000-0000-000000000003','fixture',1,'{}')$q$, 'extractions_ai_run_tenant_fk'),
  ('decision belongs to the case pack', $q$INSERT INTO policy_decisions (tenant_id, case_id, pack_id, trace_id, decision, rule_id, rule_source, reason, policy_version)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000001',gen_random_uuid(),'ignore','fixture','default','fixture','1')$q$, 'policy_decisions_case_pack_fk'),
  ('decision message belongs to its case', $q$INSERT INTO policy_decisions (tenant_id, case_id, message_id, pack_id, trace_id, decision, rule_id, rule_source, reason, policy_version)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003',gen_random_uuid(),'ignore','fixture','default','fixture','1')$q$, 'policy_decisions_msg_case_fk'),
  ('draft reply message belongs to its case', $q$INSERT INTO drafts (tenant_id, case_id, in_reply_to_message_id, channel, recipient, body)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','email','fixture@example.test','fixture')$q$, 'drafts_msg_case_fk'),
  ('draft sent message belongs to its case', $q$INSERT INTO drafts (tenant_id, case_id, sent_message_id, channel, recipient, body)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002','email','fixture@example.test','fixture')$q$, 'drafts_sent_msg_case_fk'),
  ('draft decision belongs to its case', $q$INSERT INTO drafts (tenant_id, case_id, policy_decision_id, channel, recipient, body)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000007','email','fixture@example.test','fixture')$q$, 'drafts_decision_case_fk'),
  ('draft AI run belongs to its tenant', $q$INSERT INTO drafts (tenant_id, case_id, ai_run_id, channel, recipient, body)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000006','email','fixture@example.test','fixture')$q$, 'drafts_ai_run_tenant_fk'),
  ('action message belongs to its case', $q$INSERT INTO actions (tenant_id, case_id, message_id, trace_id, idempotency_key, tool_key, risk_level, status)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000002',gen_random_uuid(),'scope','po_lookup','read','proposed')$q$, 'actions_msg_case_fk'),
  ('action decision belongs to its case', $q$INSERT INTO actions (tenant_id, case_id, policy_decision_id, trace_id, idempotency_key, tool_key, risk_level, status)
    VALUES ('10000000-0000-0000-0000-000000000001','92000000-0000-0000-0000-000000000003','92000000-0000-0000-0000-000000000007',gen_random_uuid(),'scope','po_lookup','read','proposed')$q$, 'actions_decision_case_fk'),
  ('dead letter resolver belongs to its tenant', $q$INSERT INTO dead_letters (tenant_id, source, payload, error, resolved_by)
    VALUES ('10000000-0000-0000-0000-000000000001','fixture','{}','fixture','91000000-0000-0000-0000-000000000003')$q$, 'dead_letters_resolver_tenant_fk'),
  ('dead letter with a resolver requires a tenant', $q$INSERT INTO dead_letters (source, payload, error, resolved_by)
    VALUES ('fixture','{}','fixture','91000000-0000-0000-0000-000000000003')$q$, 'dead_letters_resolver_requires_tenant')
) AS checks(label, statement, constraint_name);
