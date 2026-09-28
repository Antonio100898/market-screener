# Reuse decision

This increment preserves P-02, P-03, P-04, and P-09. The retained SEC files
remain shared official evidence. Existing Company Facts values stay authoritative,
and every added value retains its filing identity and exact source anchor.

`EdgarClient` remains the SEC HTTP owner. `sync._retain_source_bytes` remains the
atomic file writer. `sources.inline_xbrl.parse_inline_xbrl` remains the retained
file parser. `EvidenceLoader.load` remains the evidence assembly boundary, and
`sync._derive_evidence` remains the derivation boundary.

Add a bounded `sync` job for explicit CIKs. It discovers the latest direct
20-F/40-F from SEC submissions, retains the filing index and parser inputs under
the SEC cache, verifies them, then atomically activates one CIK manifest.
`EvidenceLoader` reads that manifest and performs a missing-only merge. The
process worker uses the same pure loader helper because it cannot hold SQLite.

No second parser, canonical mapping, statement selector, or snapshot writer is
needed. Incorporated exhibits remain a named unsupported relationship.
