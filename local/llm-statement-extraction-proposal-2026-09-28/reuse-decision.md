# Reuse decision

Affected product decisions: P-02, P-03, P-04, P-09, and P-17.

## Existing owners

- `api/screener/evidence.py::EvidenceLoader.load` assembles Company Facts, dimensioned facts, and
  security-cover evidence.
- `api/screener/normalize.py::build_snapshot` reads the canonical fact shape.
  `_reject_foreign`, `_current_supported_foreign_annual`, and `_balance_currency_in_filing` own the
  standard-basis and coherent-currency gate. `_ifrs_as_us_gaap` owns IFRS concept translation and
  `_fact` owns `Fact` and `Provenance` construction.
- `api/screener/sources/edinet_mapper.py::build_edinet_companyfacts` is the reusable example for
  emitting `facts.canonical` entries with exact source tag, unit, document, period, form, and
  accession.
- `api/screener/sync.py::_derive_evidence` is the only production derivation and serialization
  entry point.
- `api/screener/object_store.py::ImmutableObjectStore.put_verified` and
  `api/screener/artifacts.py::store_evidence` own content-addressed S3 storage, readback
  verification, and `evidence_artifact` registration.
- `api/screener/company_import.py::CompanyImporter._retain` proves the write-read-verify flow.
- `api/screener/postgres.py` and Alembic own PostgreSQL. The implemented tables cover immutable
  evidence artifacts and company snapshots. Extraction runs and fact observations exist only in
  `docs/production-architecture.md`; they are not implemented.

## Choice

Add one fallback before canonical derivation. It accepts retained official filing bytes and an
explicit missing-field request. Its output remains an immutable candidate. A deterministic
verifier may translate an accepted candidate into the existing `facts.canonical` entry shape, then
call `_derive_evidence` unchanged.

Do not add a second normalizer, write model values into snapshots, or let model output select the
published fact. Reuse the S3 artifact owner for the source, exact request, exact response, parsed
candidate, and verifier report. A later implementation needs immutable extraction-run and
fact-observation records before any candidate can enter selection.
