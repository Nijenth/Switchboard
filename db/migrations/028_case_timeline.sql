BEGIN;

-- Like the existing reporting views, this is an internal cross-tenant projection.
CREATE VIEW public.v_case_timeline WITH (security_barrier = true) AS
WITH events AS (
  SELECT case_id, tenant_id, created_at, 'message' AS event_kind,
         '{}'::jsonb AS summary
    FROM public.messages
  UNION ALL
  SELECT case_id, tenant_id, created_at, 'classification',
         jsonb_build_object('intent', intent_key, 'confidence', confidence, 'flags', flags)
    FROM public.classifications
  UNION ALL
  SELECT case_id, tenant_id, coalesce(executed_at, created_at),
         CASE WHEN risk_level = 'read' THEN 'lookup' ELSE 'action' END,
         CASE WHEN risk_level = 'read' THEN jsonb_build_object(
           'tool_key', tool_key, 'status', status,
           'result_reason', CASE
             WHEN tool_key = 'order_identity_check' AND output->'identity_verified' = 'true'
               THEN 'identity_verified'
             WHEN tool_key = 'order_identity_check' AND output->'identity_verified' = 'false'
               THEN 'identity_unverified'
             WHEN tool_key = 'order_lookup' AND output->>'order_status' IN (
               'pending','paid','processing','shipped','delivered','cancelled',
               'refunded','partially_refunded','payment_failed'
             ) THEN output->>'order_status'
             WHEN tool_key = 'order_refund_eligibility' AND output->>'reason' IN (
               'eligible','not_delivered','window_expired','already_refunded','order_not_found'
             ) THEN output->>'reason'
           END
         ) ELSE jsonb_build_object(
           'tool_key', tool_key, 'risk_level', risk_level, 'status', status,
           'amount', amount, 'currency', currency
         ) END
    FROM public.actions
  UNION ALL
  SELECT case_id, tenant_id, created_at, 'policy_decision',
         jsonb_build_object('rule_id', rule_id, 'rule_source', rule_source, 'decision', decision)
    FROM public.policy_decisions
  UNION ALL
  SELECT case_id, tenant_id, updated_at, 'draft',
         jsonb_build_object('status', status)
    FROM public.drafts
  UNION ALL
  SELECT case_id, tenant_id, created_at, 'approval',
         jsonb_build_object('required_role', required_role, 'decision', 'pending')
    FROM public.approval_requests
  UNION ALL
  SELECT r.case_id, d.tenant_id, d.decided_at, 'approval',
         jsonb_build_object('required_role', r.required_role, 'decision', d.decision)
    FROM public.approval_decisions d JOIN public.approval_requests r
      ON (r.tenant_id, r.id) = (d.tenant_id, d.request_id)
  UNION ALL
  SELECT coalesce(c.id, m.case_id, a.case_id, r.case_id, d.case_id, p.case_id, x.case_id, cl.case_id),
         l.tenant_id, l.created_at, 'audit',
         jsonb_build_object('audit_event', l.event_type, 'audit_sequence', l.chain_seq)
    FROM public.audit_log l
    LEFT JOIN public.cases c ON l.entity_type = 'case' AND (c.tenant_id,c.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.messages m ON l.entity_type = 'message' AND (m.tenant_id,m.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.actions a ON l.entity_type = 'action' AND (a.tenant_id,a.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.approval_requests r ON l.entity_type = 'approval_request'
      AND (r.tenant_id,r.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.drafts d ON l.entity_type = 'draft' AND (d.tenant_id,d.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.policy_decisions p ON l.entity_type = 'policy_decision'
      AND (p.tenant_id,p.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.extractions x ON l.entity_type = 'extraction' AND (x.tenant_id,x.id) = (l.tenant_id,l.entity_id)
    LEFT JOIN public.classifications cl ON l.entity_type = 'classification'
      AND (cl.tenant_id,cl.id) = (l.tenant_id,l.entity_id)
)
SELECT case_id, tenant_id, created_at, event_kind,
       summary->>'intent' AS intent, (summary->>'confidence')::numeric AS confidence,
       array(SELECT jsonb_array_elements_text(summary->'flags')) AS flags,
       summary->>'tool_key' AS tool_key, summary->>'status' AS status,
       summary->>'result_reason' AS result_reason, summary->>'rule_id' AS rule_id,
       summary->>'rule_source' AS rule_source, summary->>'decision' AS decision,
       summary->>'risk_level' AS risk_level,
       (summary->>'amount')::numeric AS amount, summary->>'currency' AS currency,
       summary->>'required_role' AS required_role, summary->>'audit_event' AS audit_event,
       (summary->>'audit_sequence')::bigint AS audit_sequence
  FROM events WHERE case_id IS NOT NULL
 ORDER BY created_at, audit_sequence, event_kind, case_id;

REVOKE ALL ON public.v_case_timeline FROM PUBLIC;
GRANT SELECT ON public.v_case_timeline TO app_reporting;

COMMIT;
