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

## Probe results

Run `python -m pytest -qs tests/test_pii_probes.py` from `services/core`.
The redactor scores **100% on the author-written probe set**, with 77/77 exact
span matches and 36/36 negative controls intact. This includes eight multiline
sign-offs in English, French and German and two new sign-off negatives. Some
author-written controls exercise explicit exceptions in the rules.

### Original author-written set

The original 69 positives and 34 negatives remain in the regression suite.
The following counts compare those same rows before and after the initial
rules change.

| Kind | Before exact matches | After exact matches | Negative preservation, both runs |
|---|---:|---:|---:|
| Name | 0/22 | 22/22 | 12/12 |
| Street | 2/17 | 17/17 | 5/5 |
| Postcode | 0/10 | 10/10 | 5/5 |
| Phone | 7/10 | 10/10 | 5/5 |
| Email | 5/5 | 5/5 | 4/4 |
| Card | 5/5 | 5/5 | 3/3 |
| Overall | 19/69 (27.5%) | 69/69 (100%) | 34/34 (100%) |

| Language | Before exact matches | After exact matches | Negative preservation, both runs |
|---|---:|---:|---:|
| English | 6/15 | 15/15 | 9/9 |
| French | 3/13 | 13/13 | 6/6 |
| Spanish | 3/13 | 13/13 | 6/6 |
| German | 4/15 | 15/15 | 6/6 |
| Portuguese | 3/13 | 13/13 | 7/7 |

The v3 offline rerun matched the previous 220/220 policy decisions per alias.
Both aliases recorded zero gate violations and restricted-tool exposures.
All three inherited synthetic PII probes passed.

### Held-out set

The same outcomes occurred before and after the sign-off fix. The test
reports these results separately without gating on them.

| Kind | Exact-span matches | Negative preservation |
|---|---:|---:|
| Name | 0/2 | 0/2 |
| Postcode | 0/1 | 0/2 |
| Phone | 0/1 | — |
| Street | 0/1 | — |
| Overall | 0/5 | 0/4 |

## Limits

These probe sets are too small and selective to estimate recall or precision
on customer traffic. Known misses include a letter suffix on an English
house number, a UK postcode after the city, a UK mobile number in local
format, a lowercase name after an introduction cue, and a name introduced by
“This is … writing.” Known false positives include order and points counts
before title-cased words and title-cased phrases such as “Customer Service”
and “Size Medium” after “for.” If recall on an independently labeled set is
insufficient, evaluate a local pretrained multilingual named entity recognition
model and retain country-specific address parsing where needed.
