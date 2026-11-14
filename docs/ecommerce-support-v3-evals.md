# E-commerce support v3 offline evaluation

Run `bash scripts/test_core.sh --offline-ai` with the pinned offline images in
the local Docker cache. The harness loads the seeded v3 enrollment and runs each
of the 110 cases twice through each of the two offline provider aliases.

**Offline intent accuracy uses a fake model that echoes expected intents. It is
not a measure of AI quality.** Decision and gate results come from the real
policy engine given that expected intent and the fixture inputs. The decision
pass models successful citation and output checks, with verification taken
from the case tags. Inbound text still goes through the real injection and
legal-threat checks. The unverified gate probes the production lookup proposal
and draft-evidence functions against rolled-back unverified cases. It checks
whether restricted tools or their evidence can be exposed in those cases.

| Result | OpenAI alias | Anthropic alias |
|---|---:|---:|
| Correct decisions | 220/220 (100%) | 220/220 (100%) |
| Intent matches among classified attempts | 182/182 | 182/182 |
| Guardrail escalations before classification | 19 cases | 19 cases |
| Agreement between repeats | 100% | 100% |
| Auto decisions on injection, money, or must-escalate cases | 0 | 0 |
| Unverified cases exposing restricted tool data | 0/14 | 0/14 |

The two aliases agreed on every case. The inherited synthetic PII probes still
reported three misses, two names and one street address.

## Results by tag

Both aliases returned the same counts below. Correct decisions count both
attempts for one alias. Violations count cases with a failure in either
attempt. Cases can belong to multiple rows. Channel, expected intent, and
expected decision also appear as groups.

| Tag or group | Cases | Correct decisions | Gate violations | Restricted tool leaks |
|---|---:|---:|---:|---:|
| `account` | 8 | 16/16 | 0 | 0 |
| `auto_reply` | 5 | 10/10 | 0 | 0 |
| `complaint` | 5 | 10/10 | 0 | 0 |
| `de` | 2 | 4/4 | 0 | 0 |
| `email` | 87 | 174/174 | 0 | 0 |
| `es` | 2 | 4/4 | 0 | 0 |
| `expected_auto` | 17 | 34/34 | 0 | 0 |
| `expected_escalate` | 19 | 38/38 | 0 | 0 |
| `expected_ignore` | 6 | 12/12 | 0 | 0 |
| `expected_needs_approval` | 68 | 136/136 | 0 | 0 |
| `fr` | 4 | 8/8 | 0 | 0 |
| `indirect` | 4 | 8/8 | 0 | 0 |
| `injection` | 15 | 30/30 | 0 | 0 |
| `money` | 22 | 44/44 | 0 | 0 |
| `multi_intent` | 7 | 14/14 | 0 | 0 |
| `must_escalate` | 19 | 38/38 | 0 | 0 |
| `negative` | 1 | 2/2 | 0 | 0 |
| `non_english` | 15 | 30/30 | 0 | 0 |
| `obfuscated` | 4 | 8/8 | 0 | 0 |
| `order_status` | 16 | 32/32 | 0 | 0 |
| `other` | 18 | 36/36 | 0 | 0 |
| `payment_issue` | 8 | 16/16 | 0 | 0 |
| `policy_lookup` | 4 | 8/8 | 0 | 0 |
| `product_question` | 12 | 24/24 | 0 | 0 |
| `pt` | 1 | 2/2 | 0 | 0 |
| `returns_refunds` | 20 | 40/40 | 0 | 0 |
| `shipping_delay` | 17 | 34/34 | 0 | 0 |
| `spam_or_auto` | 6 | 12/12 | 0 | 0 |
| `synthetic_pii` | 3 | 6/6 | 0 | 0 |
| `typo` | 9 | 18/18 | 0 | 0 |
| `unverified` | 14 | 28/28 | 0 | 0 |
| `verified` | 25 | 50/50 | 0 | 0 |
| `web_chat` | 23 | 46/46 | 0 | 0 |
