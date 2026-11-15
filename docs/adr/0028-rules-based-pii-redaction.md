# ADR 0028 — Rules-based PII redaction

Extend the local redactor with bounded name cues for English, French, Spanish,
German and Portuguese, street and house-number patterns, and postcode patterns
that require a city candidate. Normalize before matching and accept known names
as a list. Ingest supplies the sender name it already has. Understanding and
draft generation redact stored message text again before sending it to the
model, including messages received before this change.

Drafts may still use a conventional leading first-name salutation. The rest of
the draft retains the same privacy checks and the entire draft still needs exact
cited evidence for automatic handling. A detected name outside that salutation,
a street address, or a postcode can now withhold a draft that previously passed
the privacy check.

## Measured results

Run `python -m pytest -qs tests/test_pii_probes.py` from `services/core`. The
fixed synthetic set contains 69 single-span positives and 34 negative controls.
Recall requires the exact span and replacement label. The precision proxy is
negative preservation, the share of negative rows left byte-for-byte intact.

| Kind | Before recall | After recall | Negative preservation, both runs |
|---|---:|---:|---:|
| Name | 0/22 | 22/22 | 12/12 |
| Street | 2/17 | 17/17 | 5/5 |
| Postcode | 0/10 | 10/10 | 5/5 |
| Phone | 7/10 | 10/10 | 5/5 |
| Email | 5/5 | 5/5 | 4/4 |
| Card | 5/5 | 5/5 | 3/3 |
| Overall | 19/69 (27.5%) | 69/69 (100%) | 34/34 (100%) |

| Language | Before recall | After recall | Negative preservation, both runs |
|---|---:|---:|---:|
| English | 6/15 | 15/15 | 9/9 |
| French | 3/13 | 13/13 | 6/6 |
| Spanish | 3/13 | 13/13 | 6/6 |
| German | 4/15 | 15/15 | 6/6 |
| Portuguese | 3/13 | 13/13 | 7/7 |

The v3 offline rerun matched the previous 220/220 policy decisions per alias.
Both aliases recorded zero gate violations and restricted-tool exposures.
All three inherited synthetic PII probes passed.

## Limits

These probes exercise specified formats. They do not estimate recall or precision
on customer traffic. Uncued names, unconventional casing, unsupported address
formats and ambiguous sequences can still be missed or masked. If recall on an
independently labeled set is insufficient, evaluate a local pretrained
multilingual named entity recognition model against the rules and retain
country-specific address parsing where needed.
