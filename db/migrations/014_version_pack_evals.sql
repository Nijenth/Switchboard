BEGIN;
ALTER TABLE public.eval_cases ADD COLUMN pack_version integer NOT NULL DEFAULT 1;
ALTER TABLE public.eval_cases
  DROP CONSTRAINT eval_cases_pack_key_case_key_key,
  ADD CONSTRAINT eval_cases_pack_version_case_key UNIQUE (pack_key, pack_version, case_key),
  ADD CONSTRAINT eval_cases_pack_version_fk FOREIGN KEY (pack_key, pack_version)
    REFERENCES public.packs (key, version);

REVOKE INSERT, UPDATE ON public.eval_cases FROM app_core;
REVOKE UPDATE ON public.packs, public.pack_intents, public.pack_tools, public.tools, public.eval_cases
  FROM app_config_loader;
COMMIT;
