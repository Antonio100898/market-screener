# Engine-187 SEC importer result

The SEC importer now retains and reads back the active Inline-XBRL manifest and
every verified source file before the existing evidence loader applies the
supplement. No-manifest SEC imports and EDINET imports are unchanged.

Artifact roles are explicit:

- `sec_inline_current_manifest`
- `sec_inline_direct_annual_*`
- `sec_inline_annual_wrapper_*`
- `sec_inline_incorporated_source_*`

The real retained-evidence verifier observed:

- 18 verified current manifests;
- 110 file-role links over 108 unique source hashes;
- 18 distinct manifest objects;
- 14 exact engine-187 SQLite payload matches;
- AHNRF, BRBI, and NXAT still derive `foreign`;
- CIB still fails because its depositary ratio is missing;
- all objects, artifact rows, and 14 snapshot identities were reused on retry.

Exact payload hashes and errors are in `real-import-results.json`.

Checks:

- focused: 75 passed, one storage-service test skipped by its existing gate;
- full Python: 851 passed, six storage-service tests skipped, one dependency
  deprecation warning;
- real 18-case verifier: passed twice with in-memory stores;
- syntax and `git diff --check`: passed.

No service, network, PostgreSQL, S3, SQLite, cache, EDINET, API, UI, migration,
commit, stash, or branch operation was performed.

Affected decisions preserved: P-02, P-03, P-04, P-09, and P-17.
