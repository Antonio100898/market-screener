# Reuse decision

The existing owner is `sources/inline_xbrl.py`: it verifies retained SEC bytes,
parses standard facts, and merges missing facts through `EvidenceLoader`. The
acquisition caller is `sync.retain_inline_statements`; normalization enters through
`normalize._fact`, and payload provenance leaves through `sync._source`.

Extend that path with a second verified manifest relationship. Keep annual filing
identity on Company Facts entries for selection. Keep the exact incorporated
source identity in `Provenance`. Do not add a company adapter, parser, or store.

Affected product decisions: P-02, P-03, P-04, P-09.
