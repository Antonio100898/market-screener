# Engine-184 integration pin review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Update four engine-184 snapshot identity pins**
Review state: **ACCEPTED — Review 1**

## Developer update

Updated only the four retained-company snapshot SHA-256 expectations for engine 184. Their payload
SHA-256 values stayed unchanged, so this pins derived snapshot identity rather than changed company
data.

Checks:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q -s tests/integration/test_company_import_storage.py
1 passed in 2.96s

$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q
786 passed, 1 warning in 10.76s
```

The warning is the existing Starlette `httpx` deprecation warning.

## Reviewer response

### Review 1 — accepted

Accepted. Only the four engine-dependent snapshot SHA-256 expectations changed; all four payload
hashes remained fixed. The reviewer independently reran the real focused integration test and
observed `1 passed`. The developer observed 786 storage-enabled Python tests. No production code
or company data changed in this correction.
