# Reuse decision

- Owner: `api/screener/sources/cover.py` parses cover class/title pairs and classifies supported
  common equity. `store.set_cover`, `EvidenceLoader.identity`, `sync._index_tickers`,
  `sync.cover_pages`, and `normalize._reject_foreign` consume that contract.
- Reuse: extend the existing table parser, symbol validation, and common-equity predicate. Do not
  add a second identity path or ticker-specific rules.
- Correct shape: the cover parser emits only real symbol/title pairs from one class block and keeps
  compound symbol cells unchanged. The evidence loader matches an exact ticker component stated in
  that cell. The foreign gate keeps rejecting non-common classes and unresolved receipt ratios.
- Decisions preserved: P-02, P-03, P-04, and P-09. No unsupported security is admitted and parser
  repairs remain distinguishable from issuer revisions.
