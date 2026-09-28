# Direct depositary ratio reuse decision

Affected decisions: P-02, P-03, P-04, P-09, and P-17.

`sources.cover.depositary_ratio` already owns filing-text ratio interpretation and is called by
`sync.cover_pages` and `EvidenceLoader.identity`. Extend it to return one positive exact `Decimal`
only when every direct ADS-to-share relation agrees. Keep compound instruments and conflicting
relations unresolved.

`sync.cover_pages` already owns bounded cover fetch, cache-first reads, atomic retention, and
structured SQLite updates. Reuse that path for exact R-report text and one primary document only
when the current ticker has one unresolved depositary class.

`CompanyImporter` already retains and links raw cover evidence. Extend its existing cache-to-S3
path to link the uniquely cached primary document as `raw_cover_primary_document` when its parsed
ratio agrees with the structured cover identity. No second parser, fetch path, schema, or admission
rule is needed.

Acceptance checks: exact direct ratios for the 20 safe cases; null for FMX, KOF, VLRS, WAVE, BBD,
BMA, and WDS; retained bytes exist before parsing; importer S3 readback matches those bytes; engine
version 183; focused and full suites pass without running the live ratio rescan or payload gates.
