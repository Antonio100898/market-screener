# Retained-evidence import developer guidance

Guidance revision: 2
Updated: 2026-09-27
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev5-review-map.md` before work. Update only the Developer update
section of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/company_import.py`
- `api/tests/test_company_import.py`
- `api/tests/integration/test_company_import_storage.py`
- Developer update in `local/dev5-review-map.md`

The accepted persistence files and current SQLite/source files are read-only. If their contract is
insufficient, return `BLOCKED:` with the exact missing field or method.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`docs/implementation-rollout.md`, `docs/production-architecture.md`,
`local/company-import-plan-2026-09-27.md`, `local/dev4-review-map.md`, and
`~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` before implementation and
`ponytail` after tracing the flow. Do not add model-facing instructions. Preserve P-02, P-03,
P-04, P-09, and P-17.

## Current increment — restartable company import

Revision 2 resolves the pytest module-name collision. Rename the integration test to
`test_company_import_storage.py`; do not add `api/tests/integration/__init__.py`.

Build one standalone Python import entry point. It imports named tickers only and never fetches the
network. ABT, NTES, and Panasonic `6752.T` are the proof set.

1. Resolve company identity and metadata from the current SQLite store. Use one stable primary
   security identifier per issuer, independent of its current ticker.
2. Read only retained files. SEC uses cached Company Facts, optional dimensioned facts, and the
   stored cover security identity. EDINET uses cached canonical facts plus every raw ZIP named by
   its report manifest. Missing retained input is a visible error, never a fetch or guess.
3. Store every exact input with `store_evidence`. Read each artifact back through
   `ImmutableObjectStore.read_verified` before parsing or deriving. Use honest roles: official API
   facts, dimensioned facts, structured cover identity, canonical EDINET facts, and raw filing.
4. Reconstruct one `EvidenceBundle` from those verified bytes and call only
   `sync._derive_evidence`. Accept only `ok` for this proof set. Do not copy normalization,
   calculation, provenance, or serialization rules.
5. Write the result with `SharedCompanyRepository.store_and_select`. Each company is its own
   transaction. A batch stopped after one company is completed by rerunning the same command.
   Repeating an identical import reuses artifact and snapshot identities.
6. Provide a CLI such as `python -m screener.company_import ABT NTES 6752.T`. Default behavior must
   not import the full universe.

The structured SEC cover row is retained derived identity, not raw filing HTML. Label it honestly;
do not claim the raw cover was retained. EDINET raw ZIPs are retained source documents.

## Checks and real evidence

- Narrow unit tests for SEC, SEC ADS, EDINET, missing retained input, failed derivation, identical
  retry, and interruption after the first company.
- Opt-in real integration test using local retained ABT, NTES, and `6752.T` files, real S3, an
  isolated migrated PostgreSQL database, and the actual SQLite evidence loader. Run twice. Prove
  stable artifact/snapshot counts and exact artifact readback.
- Prove NTES keeps accession `0001104659-26-043468` and exact ratio `5`; Panasonic keeps JPY and
  primary ordinary-share basis; all three derived payloads equal their current SQLite snapshot
  payload after canonical JSON normalization.
- Full Python suite, Alembic check, and `git diff --check`.

## Queued after review

A fresh API developer will expose selected PostgreSQL companies and run HTTP parity checks.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the full flow
trace, reuse decision, changed files, checks with raw output, real artifact/snapshot hashes and
counts, known limits, and whether another session touched an owned file. Never mark review accepted.
