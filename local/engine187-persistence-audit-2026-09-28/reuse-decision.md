# Reuse decision

## Existing owners

- `api/screener/sources/inline_xbrl.py` owns the SEC manifest contract, retained-file hash checks,
  direct/incorporated relationship checks, parsing, and merge-only-missing rule.
- `api/screener/evidence.py::EvidenceLoader` already applies one verified current SEC supplement.
- `api/screener/company_import.py::CompanyImporter` owns retained-input enumeration, S3 write/read
  verification, derivation, and immutable snapshot publication.
- `api/screener/sources/edinet_mapper.py` owns EDINET-to-canonical mapping. The importer already
  enumerates every report archive named by the canonical adapter manifest.
- `api/screener/shared_companies.py::SharedCompanyRepository` owns immutable snapshots, artifact
  links, and current selection. `api/screener/api.py` reads only that current PostgreSQL selection.

## Decision

Extend the SEC branch of `CompanyImporter`. Use the existing Inline-XBRL verifier/loader, and add
one public retained-file enumeration from the same manifest contract. Retain the manifest and all
verified files through the existing `_retain` method before publishing the snapshot.

Do not create a second SEC persistence path, another parser, an EDINET-specific database, or
ticker/company exceptions. The EDINET production path already has the required generic shape; add
Nintendo to real storage/restart coverage and import both retained supported companies.

No schema migration is needed for these increments. `evidence_artifact` is content-addressed and
`company_snapshot_artifact` already permits one hash under multiple roles. A changed manifest or
source byte changes the immutable snapshot identity; the old snapshot remains history.
