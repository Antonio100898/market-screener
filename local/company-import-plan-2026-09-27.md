# PostgreSQL company import delivery plan

## Goal

Continue the new local architecture from `HANDOFF.md`. Retained SEC and EDINET evidence must
produce the same canonical company result on this Mac, pass through verified object storage,
remain in PostgreSQL with its provenance and security basis, and be readable through FastAPI.
Eligible foreign filers must use the same generic path as domestic filers; no ticker-specific fix.

## Requirements

1. Stable identity: PostgreSQL stores issuer and priced-security identity separately. An ADS keeps
   its filing accession, title, and underlying-shares ratio. A Japanese ordinary share keeps its
   EDINET identity, ticker, currency, and security basis.
2. Retained evidence: every imported input is uploaded and read back with size and SHA-256
   verification before the snapshot can be selected.
3. One financial owner: imports call `EvidenceLoader` and `_derive_evidence`; no financial rule,
   provenance rule, or serializer is copied.
4. History and current answer: derived snapshots are immutable. A separate selection names the
   current snapshot. Reimport is idempotent and does not erase an earlier snapshot.
5. Restart: each company commits independently. Rerunning after an interrupted batch reuses
   artifacts and snapshots and completes the remaining companies.
6. API parity: FastAPI reads the selected PostgreSQL snapshot. ABT, NTES, and Panasonic `6752.T`
   match the canonical SQLite result; companies present in the preserved payload also match its
   canonical fields. NTES's prior absence is recorded as missing local cover evidence, not a
   calculation difference.
7. Foreign coverage: the generic cover scan and derivation are run for every listed foreign or
   pending row. Supported rows become visible; rows that still fail keep a concrete unsupported
   reason and are not guessed into the product.

## Correct-from-zero shape

- Source adapters and `EvidenceLoader` read retained SEC/DERA/cover or EDINET evidence and emit
  one complete `EvidenceBundle`.
- `store_evidence` writes each retained input to content-addressed object storage, verifies the
  read, and emits an artifact identity.
- `_derive_evidence` reads the verified bundle and emits the existing canonical snapshot payload.
- PostgreSQL stores stable issuer/security identity, immutable snapshot payloads, artifact links,
  and one current selection. This is where the current code stops today: only
  `evidence_artifact` exists.
- FastAPI reads the selected PostgreSQL payload. It does not normalize or calculate again.

Affected decisions: P-02, P-03, P-04, P-09, and P-17.

## Out of scope

- Importing non-`ok` SQLite snapshots or guessing missing foreign security evidence.
- Automatic schedules, correction selection, published data releases, list/filter APIs, Next.js,
  authentication, workspaces, formulas, and the product AI agent.
- Removing or switching off the SQLite application path.
- Showing a foreign security without coherent statements and exact security-basis evidence.

## Milestones

| Milestone | State | Owner | Acceptance check |
|---|---|---|---|
| 0. Restore generic foreign cover evidence on this Mac | committed (`44aed77`) | manager | 1,234 covers scanned; 912 rows became `ok`; remaining reasons recorded |
| 1. PostgreSQL identity, immutable snapshot, artifact-link, and current-selection schema | committed (`ddc2382`) | persistence developer | 7 unit, 2 real PostgreSQL upgrade cases, 700 full Python tests; Alembic at `20260927_0003` |
| 2. Restartable retained-evidence importer | committed (`f068de6`) | import developer | real S3/PostgreSQL retry: 8 artifacts, 3 snapshots, 8 links, 3 selections; exact SQLite parity |
| 3. FastAPI PostgreSQL company read and parity | committed (`f068de6`) | API developer | real HTTP: 3 x 200, exact SQLite and dashboard-field parity; 715 Python and 113 web tests |
| 4. End-to-end review | committed (records commit) | manager | live Uvicorn 3 x 200; exact SQLite/dashboard parity; service restart retained IDs and hashes |
| 5. Full-universe import contract | committed (`f068de6`) | migration developer | Review 3: incomplete receipt preserved; 29 focused and 4 real migration/import tests |
| 6. Full retained-universe import and reconciliation | committed (records commit) | manager | 6,655 exact payload matches; 270 named evidence/pending failures; restart and HTTP sample passed |
| 7. Engine-187 SEC/EDINET artifact persistence audit | accepted | migration investigator | 18 SEC manifests/110 role links; 14 importer omissions; 6,707 exact current matches; Panasonic/Nintendo exact; no migration needed |
| 8. Retain/apply engine-187 SEC evidence in importer | accepted | migration developer | 18 manifests/110 roles; 14 exact payloads; four unchanged failures; 163 objects/artifacts and 14 snapshots reused |
| 9A. Real SEC direct/incorporated plus EDINET pair storage gate | accepted | verification developer | seven companies; 35 objects/7 snapshots/36 links; exact payloads; interruption, retry, PostgreSQL/S3 restart pass |
| 9B. Full engine-187 reconciliation and API restart | queued | verification developer | 6,721 exact current selections, 274 named exclusions, 14 SEC additions and both EDINET rows via API after restart |

The owner authorized local commits. Push and merge remain unauthorized.

## Verification

- `api/.venv/bin/python -m pytest -q` for focused files named by each milestone.
- `docker compose up -d --wait postgres seaweedfs` with the checked-in local defaults.
- From `api/`: `.venv/bin/alembic -c alembic.ini upgrade head`, `current`, `heads`, and `check`.
- Real integration tests with `RUN_STORAGE_INTEGRATION=1` against PostgreSQL and SeaweedFS.
- Two identical imports plus a forced stop after the first company and successful rerun.
- API/SQLite/payload canonical JSON comparison artifacts under
  `local/company-import-e2e-2026-09-28/`.
- `git diff --check` and an independent reviewer rerun for every milestone.
