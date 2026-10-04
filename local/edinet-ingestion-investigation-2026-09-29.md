# EDINET durable ingestion investigation

## Result

Split milestone 4 into two gates. **4A** captures and reconciles EDINET document lists and source
states. **4B** downloads current supported annual XBRL, maps it through the existing adapter, and
stages a non-current candidate. Archive fetch must not begin until list identity and mutable state
are durable.

This preserves P-02, P-03, P-04, P-05, P-09, and P-17. The open freshness/outage decision still
blocks scheduling and publication, not fixture-driven ingestion.

## Reuse decision

- `sources.edinet.EdinetClient` owns authenticated list and archive requests. Extend it to expose
  exact list response bytes and safe transport metadata; keep `documents_on()` compatible.
- `SourceObservationRepository` and `DurableJobRepository` already own immutable revisions,
  lease-guarded item outcomes, overlap replay, checkpoints, and retries. Add only EDINET lookups
  needed to compare one original filing date.
- `source_item` can represent daily list aggregates, filing identities, operation events, and later
  archive resources without a migration.
- `sources.edinet_mapper.build_edinet_companyfacts`, `sync._merge_edinet_facts`,
  `sync._derive_evidence`, and `SharedCompanyRepository.store_candidate` remain the 4B owners. Do
  not add another mapper or calculation path.

## Source contract

The June 2026 official EDINET API v2 specification says current-day lists append submissions,
withdrawal notices, metadata-edit events, and disclosure start/release events. Older original-date
lists mutate for expiry, withdrawal, metadata edits, and disclosure state.

- `withdrawalStatus`: `1` withdrawal notice, `2` withdrawn target, `0` otherwise.
- `docInfoEditStatus`: `1` edit event, `2` edited target, `0` otherwise. The document bytes do not
  change for a metadata edit.
- `disclosureStatus`: `1` disclosure stopped, `2` hidden target, `3` disclosure restored, `0`
  otherwise. Hidden XBRL may be unavailable.
- `legalStatus`: `1` normal viewing, `2` extended viewing, `0` viewing expired. Expiry is not a
  withdrawal or proof that retained financial facts are invalid.
- `parentDocID` identifies the affected parent for withdrawals and other defined relationships.
  `opeDateTime` timestamps metadata-edit and disclosure events. API timestamps are Japan time.

Persist the exact daily JSON as one mutable aggregate revision. Filing items use stable `docID`
identity and retain full row metadata. Operation events use separate stable keys so an event cannot
replace the filing's current state. A complete reconciled list may mark a missing prior row
`unavailable` with an unknown/expired-list reason; only explicit withdrawal evidence marks it
`removed`.

## 4A acceptance

- Strict bounded date parameters, exact byte retention, secret-free URLs, and restart checkpoints.
- New annual/amended rows, duplicate overlap, metadata edits, disclosure start/release, withdrawal
  notice/target, parent links, expiry, and missing-row reconciliation persist distinct immutable
  observations.
- Invalid JSON, result counts, status combinations, timestamps, identifiers, or partial responses
  cannot checkpoint or infer removal.
- Present supported annual XBRL rows enqueue one stable future `edinet-resource-fetch` child; 4A
  does not download it, derive facts, stage a snapshot, select current data, or publish.
- Real PostgreSQL/S3 proves exact readback, revision history, replay, restart, and stale-owner
  rejection. No schema change is expected.
