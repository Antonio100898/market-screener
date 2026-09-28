# Reuse decision

`CompanyImporter.import_ticker()` already owns the full retained-evidence path: prepare SEC or
EDINET evidence, derive the canonical payload, retain content-addressed objects, and call
`SharedCompanyRepository.store_and_select()`. The existing real integration test is its caller.

This gate extends that test with the retained AERO, CNI, Panasonic, and Nintendo inputs. It reuses
`inline_xbrl.current_retained_artifacts()` to copy and verify only the files named by each active
manifest. No second importer, parser, storage path, or company-specific production rule is added.
