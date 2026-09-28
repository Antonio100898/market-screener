# Engine-184 integration pin developer guidance

Guidance revision: 2
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev13-review-map.md`. Edit only the owned test and Developer update.
Do not commit, stash, switch branches, or edit production code.

## Owned paths

- `api/tests/integration/test_company_import_storage.py`
- Developer update in `local/dev13-review-map.md`

## Current increment

Engine 184 intentionally changed immutable snapshot identity. The real storage test observes CATO
payload SHA-256 unchanged at `4d757a5939529317b050aa734c31e98330e101afe7a3596bf34d189e0363dd35`
and snapshot SHA-256 changed to
`494315f1305c6ce836feaca2ece15b0db95860b33953438bdceaf448276fdc9a`.

Update the four stale snapshot hash expectations. The focused rerun proved the sibling values:

- ABT: `f97824c4050a15cae983cb6f2ac030466833263551c7175cc60a176ea516e404`
- NTES: `cc87396f1c4e64fa2d014fb609deae34f0e24020661161be68f61f2affb33225`
- 6752.T: `fe5197dae8f687ca37229928c8bfe6c7d33f0c3a058f8e7e78de0a795e952877`
- CATO: `494315f1305c6ce836feaca2ece15b0db95860b33953438bdceaf448276fdc9a`

All four payload hashes stayed unchanged. Invoke `bug-fix` and `ponytail`; explain why this
is an engine-identity pin rather than changed company data. Run the focused real integration test
and the full storage-enabled Python suite. Return exact results. Do not run source or payload jobs.

## Return protocol

Set Developer state to `READY_FOR_REVIEW` or `BLOCKED_EXTERNAL`, report the one-line change and raw
checks, and never mark review accepted.
