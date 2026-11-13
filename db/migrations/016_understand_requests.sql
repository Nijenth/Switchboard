BEGIN;

INSERT INTO public.pack_intents (pack_id, intent_key, description, default_priority)
SELECT id, 'system_escalation', 'Local guardrail escalated before model classification', 'urgent'
FROM public.packs
ON CONFLICT (pack_id, intent_key) DO NOTHING;

CREATE TABLE public.understand_requests (
  tenant_id uuid NOT NULL,
  case_id uuid NOT NULL,
  message_id uuid NOT NULL,
  pack_id uuid NOT NULL,
  rerun_key uuid NOT NULL,
  result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
  PRIMARY KEY (tenant_id, message_id, pack_id, rerun_key),
  FOREIGN KEY (tenant_id, case_id, message_id) REFERENCES public.messages (tenant_id, case_id, id),
  FOREIGN KEY (tenant_id, case_id, pack_id) REFERENCES public.cases (tenant_id, id, pack_id)
);

ALTER TABLE public.understand_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON public.understand_requests TO app_core
  USING (tenant_id = public.app_current_tenant())
  WITH CHECK (tenant_id = public.app_current_tenant());
GRANT SELECT, INSERT ON public.understand_requests TO app_core;
GRANT UPDATE (result) ON public.understand_requests TO app_core;
COMMIT;
