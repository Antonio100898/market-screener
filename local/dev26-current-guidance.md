# Developer 26 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev26-review-map.md`. Verification/runtime-data only. Do not edit code,
tests, plans, owner decisions, prompts, or credentials. Do not commit, stash, or switch branches.

## Paths you own

- `local/raw-sec-engine187-e2e-rerun-2026-09-28/**`
- Developer update in `local/dev26-review-map.md`

Runtime mutation is authorized only through named production commands against current SQLite/SEC
cache and generated dashboard payload.

## Current increment

Resume the full gate from the preserved baseline:

`/tmp/market-screener-engine187.zVa9RR/dashboard-engine185.json`

Expected SHA-256:
`d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`.

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail` for scope.
2. Confirm baseline identity and disk/RAM. One heavy command at a time; stop below 1 GiB free.
3. Rerun bounded `sync inline` for the same 18 CIKs named in dev23 guidance. Require all 18
   `activated` or `reused`; CNI incorporated, the other 17 direct. Verify manifests/hashes and that
   every row is dirty or already engine-stale. Run again and require all 18 `reused` with no new
   dirty transition.
4. Run `make regress ARGS='--all --baseline <preserved>'`. Record and explain every shared-row
   field move. No unexplained move may continue.
5. Run `make derive`, `make export`, `make audit`, and `make audit-filings`, one at a time.
6. Compare the new UI payload with the preserved engine-185 payload across identities and every
   field. Expected additions: AERO, AGMB, ALPS, AUGO, CNI, DAVI, GCDT, GMTL, HBNB, PAYP, PICS,
   TMCR, VMET, YMAT. Expected statement-solved but still excluded: AHNRF, BRBI, NXAT, CIB with their
   exact cover/ratio reasons. Any different identity delta blocks acceptance.
7. Verify all 14 additions have exact annual/source provenance, criteria/verdicts, financials,
   ratios, profiles, notes, and unique ticker/CIK. CNI must expose 6-K source plus 40-F annual fields.
   No unsupported alias/OTC row may enter.
8. Record quote/time/FX movements separately. Mandatory source, arithmetic, and filing audits must
   report zero wrong.
9. Write complete logs, identities, delta, target rows, exclusion checks, resources, and README under
   the owned evidence directory. Remove only the temporary baseline after every comparison is done;
   record its hash first. Return first divergence without code changes.

## Return protocol

Update only Developer update in `local/dev26-review-map.md`. Report raw commands/counts, exact
payload delta, audits, provenance, resource limits, and concurrent edits. Never mark accepted.
