-- Installed by scripts/check_db only for the marked, disposable core integration cluster.
-- Read text straight from its source so rows sharing a timestamp remain distinct.
CREATE VIEW public.demo_case_timeline WITH (security_barrier = true) AS
SELECT t.*, NULL::text AS body_redacted, NULL::text AS draft_body
  FROM public.v_case_timeline t
 WHERE t.event_kind NOT IN ('message', 'draft')
UNION ALL
SELECT case_id, tenant_id, created_at, 'message',
       NULL, NULL, ARRAY[]::text[], NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL, NULL, body_redacted, NULL
  FROM public.messages
UNION ALL
SELECT case_id, tenant_id, updated_at, 'draft',
       NULL, NULL, ARRAY[]::text[], NULL, status, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, body
  FROM public.drafts;

REVOKE ALL ON public.demo_case_timeline FROM PUBLIC;
GRANT SELECT ON public.demo_case_timeline TO app_reporting;
