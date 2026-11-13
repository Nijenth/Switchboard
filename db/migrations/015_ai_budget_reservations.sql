BEGIN;
ALTER TABLE public.ai_runs
  ADD COLUMN reserved_cost_usd numeric(12,6) NOT NULL DEFAULT 0 CHECK (reserved_cost_usd >= 0),
  ADD COLUMN accounting_pending boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT ai_runs_pending_has_reservation CHECK (NOT accounting_pending OR reserved_cost_usd > 0);
COMMIT;
