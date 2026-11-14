# E-commerce support v3

Northwind's email and web chat pack covers order status, shipping, returns, and
product questions. Order details require verified identity. A manager reviews
eligible refunds. Version 3 keeps the v2 prompts, rules, tools, schemas, and
knowledge and expands its 66 cases to 110. The 44 new cases name the deciding
rule in their notes.

Follow the [10-minute mock demo](../../docs/demo.md) and see the
[offline evaluation report](../../docs/ecommerce-support-v3-evals.md).

| Coverage | Cases | Coverage | Cases |
|---|---:|---|---:|
| Typos | 9 | Multiple intents | 7 |
| Non-English | 15 | Web chat | 23 |
| Spam or auto-reply intent | 6 | Expected auto | 17 |
| Order-status intent | 16 | Shipping-delay intent | 17 |
| Injection | 15 | Unverified | 14 |
| Must escalate | 19 | Money | 22 |
| Indirect injection | 4 | Obfuscated injection | 4 |

New language-tagged cases include French (4), Spanish (2), German (2), and
Portuguese (1). Counts overlap. The report lists all tags and totals.
