# 4B reuse decision

P-02, P-03, P-04, P-05, P-09, and P-17 govern this slice.

`EdinetListDiscoveryHandler` already creates one immutable fetch job tied to the latest present
filing observation. `EdinetClient` owns archive transport. `store_evidence` and
`SourceObservationRepository` own exact retained bytes and filing lifecycle checks.
The existing JPX listing map owns current TSE eligibility. The retained importer and durable path
share one whole-ZIP integrity check.
`sources.edinet_mapper.build_edinet_companyfacts` owns EDINET XBRL mapping,
`sync._merge_edinet_facts` owns replacement of the same report while retaining earlier reports,
and `sync._derive_evidence` remains the only snapshot derivation entry point.
`SharedCompanyRepository.store_candidate` owns lease-guarded, non-current snapshot storage.

4B adds one handler for the existing `edinet-resource-fetch` job. It validates the cited filing is
still the latest present revision and currently listed, retains and records the exact ZIP, validates
every ZIP member, maps and merges through the existing owners, retains the merged canonical facts,
then stages one candidate without touching `current_company_snapshot`. It adds no parser,
calculation path, publication rule, schema, or schedule.
