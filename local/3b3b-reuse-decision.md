# 3B3b reuse decision

P-02, P-03, P-04, P-05, P-09, and P-17 govern this slice.

`SecResourceFetchHandler` owns the four validated SEC roots and already records exact bytes through
`store_evidence` and `SourceObservationRepository`. `sync._inline_documents` and
`sync._archive_base` own the bounded Inline-XBRL file selection. `cover.securities` and
`cover.depositary_ratio` own listed-security evidence. `inline_xbrl.parse_inline_xbrl` and
`merge_missing_facts` own deterministic statement recovery. `sync._derive_evidence` remains the
only snapshot derivation entry point. `SharedCompanyRepository` owns snapshot identity and artifact
links.

The new work extends that path. Root fetch enqueues one stable candidate-stage child keyed by its
four immutable root observations. The child reads those exact verified objects, fetches only the
index-selected leaf graph, assembles the existing evidence bundle, derives through the existing
engine, and calls a lease-guarded `store_candidate` that does not touch `current_company_snapshot`.
No second parser, calculation path, publication rule, or schema is needed.
