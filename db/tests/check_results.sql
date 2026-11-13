-- ---------------------------------------------------------------------
\echo
SELECT current_setting('test.pass') AS passed, current_setting('test.fail') AS failed \gset
\echo 'Passed: ' :passed '   Failed: ' :failed
DO $$ BEGIN
  IF current_setting('test.fail')::int > 0 THEN
    RAISE EXCEPTION '% safety check(s) failed', current_setting('test.fail');
  END IF;
END $$;
