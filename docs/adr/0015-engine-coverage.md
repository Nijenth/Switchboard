# ADR 0015 — Measure engine coverage in CI

**Status:** Accepted

**Date:** 2026-11-14

Use pytest-cov 7.1.0 in the core development environment. The CI unit job measures statement
coverage for all of `app.engine`, prints missed lines, and fails below 90%. This complements
the offline integration tests and adds no runtime dependency.
