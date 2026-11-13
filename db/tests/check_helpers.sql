\set ON_ERROR_STOP 1
\pset footer off
\pset tuples_only on
SET client_min_messages = notice;

SELECT set_config('test.pass', '0', false), set_config('test.fail', '0', false) \g /dev/null

CREATE FUNCTION pg_temp.record(ok boolean, label text, detail text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF ok THEN
    PERFORM set_config('test.pass', (current_setting('test.pass')::int + 1)::text, false);
    RAISE NOTICE 'PASS  %', label;
  ELSE
    PERFORM set_config('test.fail', (current_setting('test.fail')::int + 1)::text, false);
    RAISE NOTICE 'FAIL  %  -- %', label, detail;
  END IF;
END $$;

-- Statement must fail with an error containing `pattern`.
CREATE FUNCTION pg_temp.expect_error(label text, stmt text, pattern text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE stmt;
  EXCEPTION WHEN OTHERS THEN
    PERFORM pg_temp.record(SQLERRM ILIKE '%' || pattern || '%', label, 'wrong error: ' || SQLERRM);
    RETURN;
  END;
  PERFORM pg_temp.record(false, label, 'no error raised');
END $$;

-- Statement must succeed.
CREATE FUNCTION pg_temp.expect_ok(label text, stmt text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE stmt;
  PERFORM pg_temp.record(true, label);
EXCEPTION WHEN OTHERS THEN
  PERFORM pg_temp.record(false, label, SQLERRM);
END $$;

-- Query must return exactly `expected` (as text).
CREATE FUNCTION pg_temp.expect_value(label text, query text, expected text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE got text;
BEGIN
  EXECUTE query INTO got;
  PERFORM pg_temp.record(got IS NOT DISTINCT FROM expected, label, format('expected %s, got %s', expected, got));
EXCEPTION WHEN OTHERS THEN
  PERFORM pg_temp.record(false, label, SQLERRM);
END $$;
