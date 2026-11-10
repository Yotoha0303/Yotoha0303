# Full dataset publication suspended

On 2026-11-10 the owner withdrew seven FeeGate data assets after confirming that
their complete public JSON sources could be retrieved outside the paid API.

Full pricing, CVE, framework, DeFi, OpenAlex, Hugging Face, and Stack Overflow
snapshots, plus the corresponding catalog index, were removed from the current
public branch trees. Local data and asset/tool configuration were preserved.
The scheduled FeeGate upload workflow is disabled and its checked-in replacement
has no schedule or upload step. The former shell upload wrapper refuses to run.

Collectors and their validation code remain available. Their output files are
ignored by Git. Do not force-add or republish complete datasets. Public samples
must be prepared deliberately and must not contain source credentials.

Resumption requires authenticated source storage, a review of FeeGate's handling
of upstream credentials, and anonymous verification across directory, REST, MCP,
and direct source access. HTTP 402 on a paid endpoint alone is insufficient.

This is a removal from current branches, not a Git history rewrite. Historical
commits, caches, forks, and already downloaded copies may retain old snapshots.
The public repository's visibility and unrelated files were not changed.
