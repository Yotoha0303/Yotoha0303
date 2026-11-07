# AI token pricing comparison

Data: **mock** · As of 2026-11-07T00:00:00+00:00.

Prices and discounts are procurement estimates. Source evidence and risk scores are unverified; utility and model quality are not benchmarked.

Input share: 0.75; CNY per USD: 7.2. Cash charges round up to fen.

| Model | Vendor / plan | Billing | Input CNY/M | Output CNY/M | Blended CNY/M | Monthly CNY | Quota / billing months | Risk / eligibility | Observed / sources |
|---|---|---|---:|---:|---:|---:|---|---|---|
| Claude 3.5 Sonnet | Synthetic Discount | tokens | 56 | 56 | 56 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| DeepSeek V3 | Synthetic Discount | tokens | 24 | 24 | 24 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| GPT-4o | Synthetic Discount | tokens | 40 | 40 | 40 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| Claude 3.5 Sonnet | Synthetic Official | tokens | 140 | 140 | 140 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| DeepSeek V3 | Synthetic Official | tokens | 60 | 60 | 60 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| GPT-4o | Synthetic Official | tokens | 100 | 100 | 100 | — | unknown / n/a | low: eligible | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| Claude 3.5 Sonnet | Synthetic Shared Account | tokens | 5 | 5 | 5 | — | unknown / n/a | high: ban_risk_high | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| DeepSeek V3 | Synthetic Shared Account | tokens | 5 | 5 | 5 | — | unknown / n/a | high: ban_risk_high | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |
| GPT-4o | Synthetic Shared Account | tokens | 5 | 5 | 5 | — | unknown / n/a | high: ban_risk_high | 2026-11-07T00:00:00+00:00 [source](https://example.org/synthetic-token-pricing) |

Synthetic simulation: row eligibility applies only to the simulated workload. Every discount signal is non-actionable.

Discount signals (>50% versus the cheapest eligible official comparable quote):

| Model | Vendor | Official reference | Discount | Savings CNY | Gate |
|---|---|---|---:|---:|---|
| Claude 3.5 Sonnet | Synthetic Shared Account | Synthetic Official | 96.4286% | 135.00 | ban_risk_high, mock_data |
| GPT-4o | Synthetic Shared Account | Synthetic Official | 95.0000% | 95.00 | ban_risk_high, mock_data |
| DeepSeek V3 | Synthetic Shared Account | Synthetic Official | 91.6667% | 55.00 | ban_risk_high, mock_data |
| GPT-4o | Synthetic Discount | Synthetic Official | 60.0000% | 60.00 | mock_data |
| Claude 3.5 Sonnet | Synthetic Discount | Synthetic Official | 60.0000% | 84.00 | mock_data |
| DeepSeek V3 | Synthetic Discount | Synthetic Official | 60.0000% | 36.00 | mock_data |

Subscription monthly prices may be amortized. Compare identical known quotas and commitments; the optimizer charges the entire billing commitment upfront.

Dataset schema: `1.0.0`; FeeGate route: `/api/token-cost-arbitrage`. Each row retains native currency prices, source URLs, timestamps and gate reasons in the JSON export.
