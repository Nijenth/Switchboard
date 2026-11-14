# E-commerce support

Northwind's email and web chat pack covers shipping, order status, returns, and common product questions. Order information requires verified identity. A manager approves refunds. Unmatched cases follow the engine's default approval path.

For the demo, compare a verified order-status request with an unverified request for the same order, then review a refund proposal. The starter evals record expected intents, decisions, and allowed or forbidden tools. Once the prompts are evaluated, report intent and decision accuracy, prohibited-tool use, latency, and cost. The prompts in this version are unevaluated.

### Refund demo (v2)

Use Northwind order `NW-1003` from its fixture sender to see a manager approval and a mock
refund receipt. The fixture also includes an in-transit order (`NW-1001`), an expired delivery
(`NW-1004`), and an already-refunded order (`NW-1007`). A missing order has no shop row and
fails the identity check before the case can access an eligibility finding.
