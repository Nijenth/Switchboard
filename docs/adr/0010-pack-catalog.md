# ADR 0010 — Load released pack versions from files

**Status:** Accepted

**Date:** 2026-11-13

Pack files and their referenced tool registry entries define the released catalog. PyYAML parses the declarative files with a safe loader, and jsonschema checks their structure and referenced contracts. The loader checks condition fields and tool permissions separately. CI uses type stubs for both dependencies.

Run the loader as `switchboard_config_loader` after migrations and before the demo seed. The demo seed only creates tenant data, enrollments, and mock records. The initial pack IDs match the original demo catalog so existing test fixtures can continue to reference them. A database containing the previous SQL-seeded catalog needs an explicit baseline review before the first file-based sync.

One transaction inserts new versions and their child rows. Each checksum includes the pack directory and the canonical definitions of its referenced registry tools. Catalog tool rows are shared across versions, so existing tool contracts are immutable. Introduce a new tool key for an incompatible contract. Eval cases belong to a pack version. A rerun of an identical version skips writes. Enrollments continue to point at their explicitly selected versions.

Pack knowledge is synchronized for an explicitly selected tenant and only for the version that tenant has enabled. Northwind's material is scoped to the Northwind demo enrollment in the setup scripts. The loader stores text and checksums. Embedding remains a later job.
