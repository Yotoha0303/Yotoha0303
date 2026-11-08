# Otoha Intelligence and Multi-Platform Campaign Strategy — Phase 1

## Executive Overview

Study how music releases connect short-form discovery, full performances,
catalog listening and recurring community participation. Apply testable
distribution patterns to Yotoha's engineering work and the AI Collaboration
Framework. The second objective is to practice FeeGate ingestion, normalization,
entity-safe search and commercial delivery on a bounded cultural dataset.
Success means attributable developer adoption and trustworthy data products.
Music audience size is a reference for research, not a forecast for software.

The subject of the cited music credits is **音羽-otoha-**. Similar spellings do
not establish identity: **をとは is an unresolved name variant in this product**.
The developer identity is independently anchored at
`https://github.com/Yotoha0303#identity`. This research implies no affiliation,
endorsement or shared identity between the artist and the developer.

The Phase 1 product includes selected works, seven platform models, three cited
anime connections, example streaming trajectories, a funnel and deterministic
entity features. It is a research preview. The official artist site supplies
YouTube, X and TikTok account cross-links. Label affiliation, current subscriber
counts, listener totals and stream rankings have not been independently
established across those seven services.

### Evidence and uncertainty

| Research item | Baseline treatment | Evidence and limits |
| --- | --- | --- |
| Bocchi the Rock!: 青春コンプレックス | Otoha composition credit; recording performer is Kessoku Band | [Official Black Butler artist profile](https://www.kuroshitsuji.tv/music/) cites this songwriting work. Preserve the distinction between composer and recording artist. |
| Black Butler: Public School Arc | Opening performer, 狂信者のパレード -The Parade of Battlers | [Official music credits](https://www.kuroshitsuji.tv/music/) name the performer and song. |
| 大生解 / ハッピー・エンド・ロール | Selected debut works, dated 2024-02-14 | The same official artist profile supplies the debut titles and date. |
| Dr. Stone: Science Future | Second-cour ending performer, no man's world | The [official artist profile](https://otohaofficial.com/profiles) supplies the anime credit; the [official release page](https://otohaofficial.com/musics/19736) dates the single to 2025-07-11. |
| YouTube, X and TikTok accounts | Cited account cross-links, not measured reach | The official artist site's profile navigation links to `@otoha_official`, `_______0108` and `@_.otoha._0108` respectively. Other platform account IDs remain unresolved. |
| Sony Music | Entertainment feature-space term | It is not evidence of a verified label contract in this dataset. |
| Audience and commercial metrics | Synthetic examples or explicit nulls | No mock figure is an observed artist measurement. Proposed prices are hypotheses. |

The official Black Butler profile also describes artist-directed video production
and a historical TikTok following above 1.4 million. The profile does not supply
a precise observation date for that claim. Phase 1 therefore does not export it
as a current follower count or use it to calculate a growth rate.

### Operating the product

From the repository root:

```bash
# Deterministic offline fixture, followed by a disk round-trip verification.
python3 scripts/otoha_intelligence_harvester.py --mock --summary --verify

# A separate output preserves the checked-in fixture.
python3 scripts/otoha_intelligence_harvester.py --mock --output /tmp/otoha-example.json

# Retrieve bounded official reference pages; analytics remain null.
python3 scripts/otoha_intelligence_harvester.py --output /tmp/otoha-live-reference.json --summary

# Read-only validation of an existing product; no network or file rewrite.
python3 scripts/otoha_intelligence_harvester.py --verify --output /tmp/otoha-example.json

python3 -m unittest scripts/test_otoha_intelligence_harvester.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

The default output is `data/products/otoha_market_intelligence.json`, resolved
from the script's repository rather than the caller's working directory.
`--mock --verify` generates and validates; `--verify` without `--mock` validates
an existing file. `--summary` reports the selected operation's product.
Exit codes are 0 for success, 1 for generation or validation failure and 2 for
invalid arguments. No command publishes data or charges a customer.

The harvester uses only Python's standard library and does not harvest local
identity documents, credentials or user configuration. Offline metrics have status
`mock`, null observation times and no external measurement source. Missing live
metrics use status `unavailable` and JSON `null`, which does not mean zero.
Live retrieval has a fixed official-source registry, a 10-second timeout per
source, a two MiB response cap and restricted redirects. A failed source is
recorded as a generic error; complete evidence failure returns a nonzero exit
without substituting synthetic data. Retrieval hashes attest to retrieved bytes,
not to the truth or completeness of every claim on a page.

## Platform Resonance Strategy

Treat each entertainment tactic as a hypothesis about distribution. Transfer
the mechanism, then measure its effect on engineering adoption. Obtain permission
for any media reused in a campaign; build developer demonstrations from original
material and preserve separate branding.

| Channel | Music distribution mechanism to study | Developer campaign experiment | Primary evidence of progress |
| --- | --- | --- | --- |
| YouTube | Short hook leads to a complete performance or music video | A 30–60 second incident diagnosis clip links to a reproducible systems walkthrough | Tagged documentation visits, example completions, returning viewers |
| X / Twitter | Timely release clips and replies sustain attention | Post one benchmark with method, source and a demonstration; answer technical questions | Interactions / impressions for a consistent post cohort; qualified destination visits |
| TikTok | Immediate visual payoff, captions and reusable formats encourage discovery | Demonstrate one visible performance or workflow improvement, then link a tutorial | Interactions / views, completed views and attributed profile-to-doc visits |
| Spotify / Apple Music | Discovery connects a single release to a durable catalog | Organize tutorials as a coherent project series with one next action per page | Documentation return rate, completed setup and subsequent example use |
| Bilibili | Subtitles and contextual explanation support local discovery and discussion | Publish a localized engineering demonstration with translated terminology and setup instructions | Runnable example completions, useful questions and accepted contributions |
| NetEase Music | Localized metadata and catalog search reduce discovery friction | Maintain translated project keywords and consistent links to canonical documentation | Search-to-doc visits and successful task completion in the localized cohort |

Bilibili and NetEase entries describe research coverage and adaptation options;
they do not assert that an official artist account or catalog has been found.
Streaming services do not expose equivalent metrics: Spotify catalog access is
not access to monthly listener analytics, and Apple artist analytics require
authorized access. Store metric definitions with their units and denominators.

Use anime tie-ins as an example of reaching an adjacent audience through a
relevant work. For software, publish a useful integration with a complementary
open-source project, give exact credit and link both projects. Record whether
the work is authored, integrated, demonstrated or merely discussed. A mention
does not constitute a partnership.

### Funnel and campaign cadence

Use a tagged journey: impression → engaged viewer → documentation visit →
successful example run → returning user or contributor. Define activation as a
successful runnable example, not a star or page view. Define retention as a
second meaningful use within a declared interval. Use aggregated, consented
analytics and avoid exporting individual audience identities.

The offline exercise uses one synthetic cohort: 100,000 impressions, 10,000
engaged users, 2,000 destination visits, 500 activations and 100 retained users.
Its engagement rate is 10%, click-through rate is 2%, activation rate is 25%
and retention rate is 20%. These numbers test arithmetic and schema behavior;
they are neither artist results nor campaign targets. The engagement measures
for X and TikTok use their own denominators and are not directly comparable.

1. **Week 1:** Choose one developer problem, publish the runnable example and
   record a reproducible baseline. Define the cohort, source tags and retention
   interval before distributing content.
2. **Week 2:** Produce a full walkthrough, two short demonstrations and a
   technical discussion post from the same result. Keep the destination and
   activation definition constant.
3. **Week 3:** Translate the best-supported demonstration and test one relevant
   integration. Compare activation within each channel and language cohort.
4. **Week 4:** Review qualified activations, retained use and cost per activated
   user. Repeat a format only when downstream results justify it.

Report uncertainty with small samples. Deduplicate only where consented
attribution supports it. Never add platform follower counts to produce unique
reach, compare cumulative plays with monthly listeners, or attribute organic
growth to a campaign without a suitable comparison. A streaming trajectory
requires at least two comparable observations on the same platform and period
definition; missing values remain null.

## FeeGate Commercial Pipeline

### Harvesting, normalization and search

1. **Register evidence:** Pin an authoritative source, canonical artist/account
   identifier, permitted collection method, relevant claim and retrieval policy.
   Use official APIs or authorized exports for analytics. Do not bypass access
   controls or infer private streams from public popularity indicators.
2. **Capture provenance:** Keep UTC retrieval time, raw-response SHA-256,
   metric observation time and measurement interval separately. A reference
   citation is not a fresh observation. Source failures remain explicit.
3. **Normalize:** Preserve native platform units, nullable values, recording
   performer versus composition credit, anime installment and source IDs.
   Preserve track and account identifiers once confirmed. The current data is
   deliberately a selected discography rather than an exhaustive catalog.
4. **Validate and version:** Enforce the script's `PRODUCT_SCHEMA`, complete
   required platform models, referential integrity, mode labels, formulas and
   identity boundaries before writing. The records checksum covers canonical
   compact UTF-8 JSON with sorted keys. The content checksum uses the same
   encoding over the entire envelope excluding only `content_sha256` itself.
   Checksums detect accidental changes; they are not an authenticity signature.
5. **Index:** Map the normalized `records` array to a dedicated music collection.
   Use `record_id` as the document key, `entity_id` and `record_type` as filters,
   `title` and `search_terms` as searchable fields, and `data_status` as an
   explicit quality filter. Attach source IDs and schema version to deliveries.
   Maintain a separate developer collection. Feature profiles used for
   disambiguation are not public claims about the developer's music career.

The Phase 1 script creates the dataset and search documents. Running a search
service, integrating analytics credentials and scheduling collection are later
operational steps. Do not load mock records into a production revenue index.

### Packaging, price discovery and x402 distribution

Offer a free sample that exposes schema, provenance and missingness. A later
paid snapshot can add verified catalog changes and licensed aggregate analytics;
a subscription can add versioned updates. Test a proposed USD 0.05 per query
against collection cost, rights cost, update frequency and customer interviews.
It is a pricing experiment, not evidence of market demand or realized revenue.
Never charge for synthetic reach claims presented as measured intelligence.

The repository's `scripts/feegate_x402_merchant_gateway.py` can serve a reviewed
file through a configured dataset alias. A future approved deployment could
register `--dataset otoha=data/products/otoha_market_intelligence.json` and expose
`GET /api/otoha`. The product is currently marked `production_ready: false`;
the harvester neither launches that gateway nor adds the product to the legacy
catalog. A separate release gate must enforce readiness before configuring a
paid route; the gateway does not inspect this product's readiness flag for you.

For a paid deployment, the intended architecture is:

```text
Official sources / authorized analytics
  -> normalization + schema / rights / identity validation
  -> versioned JSON snapshot + isolated music search collection
  -> HTTPS FeeGate route
  -> HTTP 402 with PAYMENT-REQUIRED
  -> client retries with PAYMENT-SIGNATURE
  -> trusted facilitator verifies and settles payment
  -> settled delivery with PAYMENT-RESPONSE and snapshot checksum
```

Keep settlement verification and payment secrets in the gateway, outside public
dataset files. The existing gateway uses integer USDC units and facilitator
verification; a request header alone does not prove payment. Deliver only after
confirmed settlement. Preserve version, checksum and receipt references for
reconciliation, and do not retry an uncertain settlement as a new charge.
Production release requires confirmed subject/account resolution, permitted
redistribution rights, licensed measurements with observation intervals and a
reviewed freshness policy. Until then, this product stays an offline fixture or
research preview.

## Disambiguation & Sovereignty Guardrails

Use stable identifiers rather than phonetic similarity. Music records use
`music:otoha-jp`; the developer uses the independent GitHub identity URI.
Never put artist URLs, Sony terms, anime credits or music profiles in the
developer's `sameAs`, professional biography or project metadata. Apply the
same rule in reverse. A benchmark study does not create an affiliation.

The machine-verifiable feature order is:

```text
[j_pop, rock, anison, sony_music,
 go_backend, distributed_systems, multi_agent_frameworks, ai_collaboration_framework]
Music reference:     [1, 1, 1, 1, 0, 0, 0, 0]
Developer reference: [0, 0, 0, 0, 1, 1, 1, 1]
L1 separation: 8
```

Features are binary term evidence with case-insensitive Latin word boundaries
and explicit Japanese terms. Each domain score is its four-feature mean.
Two features in one domain and zero in the other classify the context; any
mixed evidence returns `ambiguous`, and weaker evidence returns
`insufficient_evidence`. A name alone contributes no evidence. Scores are
deterministic context indicators, not calibrated identity probabilities.
Negation, quotation and new aliases require review; this small classifier
does not replace authoritative account verification.

Quarantine ambiguous names and mixed search hits. Resolve them with official
cross-links and exact account identifiers before changing alias tables.
In particular, do not merge をとは into 音羽-otoha- based on the task's spelling
or a shared romanization. Apply the same work-and-role evidence requirement to
any new anime connection before adding it to the three cited baseline credits.

Public deliverables contain only public research facts, transparent simulations
and the public developer identity. Never include internal organization names,
private operational identifiers, credentials, local user paths or personal
geographical location. The exporter rejects restricted markers before writing
and suppresses raw network/filesystem error text. Review every release artifact
alongside these automated checks. Corrections must update the source claim,
normalized records and versioned checksum together.
