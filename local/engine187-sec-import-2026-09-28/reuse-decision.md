# Reuse decision

- `api/screener/sources/inline_xbrl.py` owns manifest, path, relationship, hash,
  size, stale-filing, parse, and merge validation.
- `api/screener/evidence.py::EvidenceLoader.load` owns applying the current
  verified supplement to Company Facts.
- `api/screener/company_import.py::CompanyImporter._retain` owns immutable
  object storage and byte-for-byte readback before snapshot publication.
- `api/screener/shared_companies.py::SharedCompanyRepository.store_and_select`
  owns snapshot identity, artifact links, history, and current selection.

Add one public artifact enumeration result to the existing Inline-XBRL owner.
The SEC importer will retain and read back those verified bytes, then call the
existing evidence loader. No parser, financial rule, database schema, EDINET
path, or company-specific branch is added.

Affected decisions: P-02, P-03, P-04, P-09, and P-17.
