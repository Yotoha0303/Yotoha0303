# Token cost arbitrage advisor — founder action briefing

As of 2026-11-07T00:00:00+00:00. Data: **mock**. Source errors: 0.

## Monthly purchasing decisions

Allocate only the required workload. The recommendation maximizes configured utility, then minimizes heuristic risk exposure, then cash spend. Keep the unspent balance.

| Budget CNY | Cash spend CNY | Unspent CNY | Utility | Pareto plans | Action |
|---:|---:|---:|---:|---:|---|
| 50.00 | 48.00 | 2.00 | 16 | 17 | SIMULATION ONLY: DeepSeek V3: 1,000,000 tokens via Synthetic Discount (CNY 24.00); GPT-4o: 600,000 tokens via Synthetic Discount (CNY 24.00) |
| 100.00 | 97.60 | 2.40 | 26 | 27 | SIMULATION ONLY: Claude 3.5 Sonnet: 600,000 tokens via Synthetic Discount (CNY 33.60); DeepSeek V3: 1,000,000 tokens via Synthetic Discount (CNY 24.00); GPT-4o: 1,000,000 tokens via Synthetic Discount (CNY 40.00) |
| 200.00 | 120.00 | 80.00 | 30 | 31 | SIMULATION ONLY: Claude 3.5 Sonnet: 1,000,000 tokens via Synthetic Discount (CNY 56.00); DeepSeek V3: 1,000,000 tokens via Synthetic Discount (CNY 24.00); GPT-4o: 1,000,000 tokens via Synthetic Discount (CNY 40.00) |

Unfilled workload (remaining configured blocks):

| Budget CNY | Model | Unfilled blocks |
|---:|---|---:|
| 50.00 | Claude 3.5 Sonnet | 10 |
| 50.00 | DeepSeek R1 | 10 |
| 50.00 | GPT-4o | 4 |
| 50.00 | Gemini 1.5 Pro | 10 |
| 100.00 | Claude 3.5 Sonnet | 4 |
| 100.00 | DeepSeek R1 | 10 |
| 100.00 | Gemini 1.5 Pro | 10 |
| 200.00 | DeepSeek R1 | 10 |
| 200.00 | Gemini 1.5 Pro | 10 |

## Discount decisions

- Claude 3.5 Sonnet / Synthetic Shared Account: 96.4286% below Synthetic Official; save CNY 135.00 for 1,000,000 tokens. Hold: ban_risk_high, mock_data.
- GPT-4o / Synthetic Shared Account: 95.0000% below Synthetic Official; save CNY 95.00 for 1,000,000 tokens. Hold: ban_risk_high, mock_data.
- DeepSeek V3 / Synthetic Shared Account: 91.6667% below Synthetic Official; save CNY 55.00 for 1,000,000 tokens. Hold: ban_risk_high, mock_data.
- GPT-4o / Synthetic Discount: 60.0000% below Synthetic Official; save CNY 60.00 for 1,000,000 tokens. Hold: mock_data.
- Claude 3.5 Sonnet / Synthetic Discount: 60.0000% below Synthetic Official; save CNY 84.00 for 1,000,000 tokens. Hold: mock_data.
- DeepSeek V3 / Synthetic Discount: 60.0000% below Synthetic Official; save CNY 36.00 for 1,000,000 tokens. Hold: mock_data.

Quotes requiring evidence or correction:

- Claude 3.5 Sonnet / Synthetic Shared Account: ban_risk_high.
- DeepSeek V3 / Synthetic Shared Account: ban_risk_high.
- GPT-4o / Synthetic Shared Account: ban_risk_high.

## Commercial intelligence

Publish the dated public comparison matrix for searches about AI token prices, CNY API costs and subscription risk. The JSON Dataset supplies citations, separate input/output prices, billing commitments and gate reasons for generative search consumers.

Offer the same public market dataset through FeeGate's paid endpoint. Do not infer resale profit from procurement discounts. Internal budgets, workload preferences and allocation frontiers are omitted from the public export.

## Calculation and risk contract

- Exact Pareto objectives: minimize cash spend, maximize completed-block utility, minimize token-weighted risk. Equal objective vectors retain one deterministic representative. Each model chooses at most one offer; quantities are whole configured blocks and cannot exceed demand or subscription quota.
- Token input count is floor(total tokens × input share); output receives the remainder. API charges include a supplied minimum purchase. Subscriptions charge monthly price × billing months upfront, while utility counts only this month's quota. No account stacking or shared cross-model subscription quota is assumed.
- Risk units = tokens × (ceil(100 − reputation) + 20 for medium ban risk). This is an explainable preference penalty, not a probability. Missing reputation/domain/ban evidence, new domains, high ban risk, unsafe source URLs, stale/future observations and source errors block purchases. Medium risk requires an explicit option. Mock discounts are always non-actionable.
- Input/output prices must share source/vendor/model/plan/currency/status and the newest timestamp. Incomplete newest snapshots block offers. Live and mock feeds must be separate. Official references require raw metadata official=true and all purchase gates; references use the cheapest comparable official quote. A 50% discount is excluded; the threshold is strictly greater.
- USD conversion requires an explicit CNY-per-USD assumption. Numeric inputs support at most 12 decimal places. Charges round up separately to fen, budgets require whole fen. Unknown subscription quotas or billing periods block allocation; conflicting original periods and enriched commitments fail validation. Taxes, provider limits beyond supplied quotas, FX changes and model quality benchmarks are outside the calculation.
- Search has explicit state/transition bounds and fails if exceeded; it never labels a truncated frontier optimal. Defaults: 10 blocks × 100,000 tokens for each crawler catalog model, utility 1 per block, input share 0.75, reputation ≥70, established domain and low ban risk, freshness ≤72 hours.

## Input and CLI

Use crawler JSON containing `records` and `errors`, or a bare array of normalized observations. The Python API also accepts crawler `PricingObservation` instances. The crawler file is consumed without modification. Its SQLite history is not used because that schema does not retain live/mock provenance.

Optional explicit subscription/prepayment evidence lives in each original source row's `advisor` object, retained as `raw.advisor` in crawler output. Fields: `monthly_token_quota` (positive integer), `billing_months` (1, 3 or 12), `minimum_purchase_cny` (nonnegative decimal). Without billing_months, exact raw period labels month/monthly, year/annual/yearly or quarter/quarterly are recognized. Do not infer token quotas from chat-message caps or unlimited marketing claims.

Workload JSON is an array such as:

```json
[{"model":"GPT-4o","max_blocks":10,"tokens_per_block":100000,"utility_per_block":2}]
```

```bash
python3 scripts/token_cost_arbitrage_advisor.py --input prices.json --usd-to-cny 7.2 --budget 50 --budget 100 --budget 200 --demand workload.json
python3 scripts/token_cost_arbitrage_advisor.py --offline --now 2026-11-07T00:00:00Z --usd-to-cny 7.2
python3 -m unittest discover -s scripts -p 'test_token_cost_arbitrage_advisor.py' -v
```

Default exports: this internal briefing, `docs/token_cost_comparison.md` (GitHub README-ready table), and `data/feegate-token-cost-arbitrage.json` (public FeeGate dataset). Override with `--briefing`, `--markdown`, `--output`. CLI exit codes: 0 complete, 1 incomplete ingestion, 2 invalid input/output/search limit.

## FeeGate delivery and public schema

```bash
python3 scripts/feegate_x402_merchant_gateway.py --dataset token-cost-arbitrage=data/feegate-token-cost-arbitrage.json --public-url https://data.example.org --amount 50000
```

This registers `/api/token-cost-arbitrage` with the existing gateway (Base USDC, $0.05 per query by default). Configure its trusted facilitator to enforce payment. Optional advisor flags `--resource-url https://data.example.org/api/token-cost-arbitrage --pay-to <gateway-recipient>` add x402 v2 PaymentRequired metadata and its base64 PAYMENT-REQUIRED value; the file itself does not enforce payment. Keep the recipient and `--payment-amount` identical to gateway configuration.

Public JSON fields: `schema_version`, `catalog_id`, `generated_at`, `data_status`, `ingestion_error_count`, `market_summary`, `records`, `opportunities`, `methodology`, `seo`, `structured_data`, `transport`, `sha256_checksum`. Monetary prices and percentages are decimal strings (unknown is null), token quantities are integers, eligibility/actionable are booleans. Missing FX never produces a zero CNY price. The checksum hashes sorted compact UTF-8 JSON excluding its own field; it detects changes and does not authenticate the publisher. Raw source payloads and source error messages are never exported.
