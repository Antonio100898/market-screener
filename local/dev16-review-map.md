# Developer 16 — review map

Developer state: **IDLE — proposal accepted after Review 1**
Current item: **LLM shadow extraction proposal**
Review state: **ACCEPTED — owner prompt approval pending**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**READY_FOR_REVIEW — guidance revision 1**

Trace: all 18 `incomplete_company_facts` rows fail before statement normalization. Their latest
20-F/40-F accession has no coherent standard balance anchor in Company Facts, so
`normalize._reject_foreign` raises `UnsupportedFilerError`. The inventory and exact hashes are in
`local/llm-statement-extraction-proposal-2026-09-28/inventory.md`.

Proposal:

- `local/llm-statement-extraction-proposal-2026-09-28/README.md`
- `local/llm-statement-extraction-proposal-2026-09-28/reuse-decision.md`
- `local/llm-statement-extraction-proposal-2026-09-28/candidate.schema.json`
- `local/llm-statement-extraction-proposal-2026-09-28/prompt.md`
- `local/llm-statement-extraction-proposal-2026-09-28/ab-plan.md`
- `local/llm-statement-extraction-proposal-2026-09-28/frozen/manifest.md`

Frozen cases: AERO classified inline IFRS; CIB unclassified bank IFRS; CNI 40-F with audited
statements incorporated from SEC-filed Exhibit 99.2. The manifest records accessions, document
hashes, missing fields, and gold anchors. Duplicate SEC working copies were removed after
inspection. Implementation registers the retained or named official bytes through
`store_evidence`, requires the manifest hash, and verifies S3 readback.

Reuse: emit verified candidates into the existing `facts.canonical` adapter shape and call
`sync._derive_evidence`. Reuse `ImmutableObjectStore.put_verified`, `store_evidence`, and
`CompanyImporter._retain`. Do not create a second normalizer or write model output into snapshots.

Checks performed: all 18 inventory rows confirmed; schema JSON parses; every cited AERO, CIB, and
CNI HTML anchor exists; CNI's 40-F contains the exact Exhibit 99.2 SEC link; all frozen hashes were
recomputed; prompt scaffolding-leak grep returned no hits; the owned-path trailing-whitespace check
passed. Full JSON Schema validation was not run because no validator is installed.

Known limits: no LLM was called, so prompt behavior is **UNVERIFIED**. No provider/model, trace,
cost, or latency exists. PostgreSQL has no extraction-run or fact-observation tables. The statement
prompt does not cover security-class, ADS-ratio, or shareholder-right evidence; that needs a
separate candidate contract and verifier.

One recommended owner decision: approve the first implementation as `SUPPLEMENT` only. It may fill
a deterministically missing field but may not override a deterministic value.

## Reviewer response

### Review 1 — proposal accepted

Accepted as an unverified proposal. The trace reaches the existing canonical adapter and immutable
artifact owners instead of adding another normalizer. AERO, CIB, and CNI cover three real SEC
statement shapes with exact hashes and gold anchors. The schema requires source, period, currency,
scale, scope, and explicit ambiguity; deterministic checks own acceptance. No LLM was called and no
runtime prompt was changed. The owner must approve `prompt.md` before implementation or A/B calls.
