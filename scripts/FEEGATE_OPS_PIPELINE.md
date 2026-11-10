# FeeGate operations pipeline

Python 3.12+ and the standard library are sufficient. All commands run from the repository root. Times in the published schema use UTC+8 and `YYYY-MM-DD HH:MM`; naive source times are interpreted as UTC+8. No requests are made to external services.

```sh
python3 scripts/feegate_ops_pipeline.py --mock --audit
python3 scripts/feegate_ops_pipeline.py --mock --dry-run
python3 scripts/feegate_ops_pipeline.py --input ../_leaf_sandboxes/feegate_ops --audit
python3 scripts/feegate_ops_pipeline.py --input ./incoming --output-dir data/products
python3 -m unittest scripts/test_feegate_ops_pipeline.py
```

Native input is one product per UTF-8 JSON file:

```json
{
  "product_id": "market-index",
  "title": "Market index",
  "category": "market",
  "updated_at": "2026-11-10 08:00",
  "records": [{"id": "item-1", "value": 12.5}],
  "x402_pricing": {"amount": 0.05, "currency": "USD", "network": "base"}
}
```

IDs accept lowercase letters, digits, underscores and hyphens, up to 96 characters. Records must be a nonempty array of nonempty objects. Amounts must be finite nonnegative JSON numbers; currency is a nonempty string and network is optional. Optional item/record counts must match exactly. Duplicate keys, non-finite numbers, invalid times, symbolic links and files over 16 MiB are rejected. Credential keys and known credential values are removed recursively, including URL user-info/query credentials, control characters and decorative symbols.

An optional native `sha256_hash` covers the complete original product excluding that field, encoded as sorted-key compact UTF-8 JSON without a trailing newline. Legacy `catalog_id` / `product_name` envelopes retain their existing record checksum conventions. An adjacent `feegate-catalog-index.json` can supply a missing category and an explicit legacy per-query dollar price. Missing commercial pricing is rejected rather than inferred.

With no input, the controlled sibling sandbox is checked first. If empty or absent, local feeds listed in `data/feegate-catalog-index.json` are used. Within a batch, the latest product timestamp wins; ties use canonical bytes for deterministic selection. Older inputs cannot roll back the catalog. Invalid batch members are reported and valid members can still publish.

Artifacts use `<product_id>-<sha256>.json`. The catalog hash covers the exact artifact bytes, including the trailing newline. Existing artifacts and catalog metadata are verified before publication. Immutable artifacts are installed first, then the catalog is replaced atomically under a directory lock. Interrupted publication can leave an unreferenced artifact, but an existing catalog continues to reference complete files. Hash checks detect changes; they do not authenticate the dataset publisher. Previous artifacts are retained for review.

`--catalog-file` selects a separate ledger. The default is `<output-dir>/feegate_catalog.json`. `--audit` and `--dry-run` perform the same validation and merge planning without writes, directory creation or locks. `--mock` uses fixed fixtures and cannot be combined with `--input`.

Exit code 0 means all inputs passed. Code 2 means an input was rejected or validation/I/O failed; inspect the JSON report before consuming a partially accepted batch.
