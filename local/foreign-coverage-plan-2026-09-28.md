# Foreign company coverage delivery plan

## Goal

Increase support for foreign companies using primary, filing-backed evidence while preserving the
existing rule: no security is shown unless its current statements, traded class, currency, and any
depositary ratio are coherent. The first step is to explain every current exclusion and identify
generic fixes. No ratio, ticker identity, or statement basis may be guessed.

## Requirements

1. Inventory every listed snapshot still marked `foreign` after the full cover scan and engine-181
   derive. Each ticker has one reproducible primary reason and current annual accession.
2. Split missing cover identity into parser defects, filings that do not register the mapped ticker,
   stale/OTC ticker mappings, and genuinely absent evidence.
3. Split missing depositary ratios into recoverable filing-cover/footnote evidence, wrong-class
   pairing, and genuinely unresolved ratios.
4. Split incoherent statements into missing standard anchors, mixed currencies, extension-only
   facts, incomplete Company Facts, and unsupported accounting bases.
5. Recommendations must name the owning layer and the smallest generic fix. Company-specific
   adapters remain explicit; no ticker exceptions in normalization or pricing.
6. Record which rows can be fixed from retained evidence, which need an official source fetch, and
   which need an owner decision or remain unsupported.
7. A ticker change is accepted only when a later official SEC filing pairs the new ticker and
   exchange with the same security class as the retained annual cover. Both immutable filings and
   the effective filing date remain linked to one stable security identity. Ambiguity stays
   excluded.
8. When deterministic statement extraction is incomplete, an LLM may create a structured candidate
   with exact source citations. Shadow mode stores the candidate and verification result but cannot
   change the published company. Publication remains fail-closed behind deterministic checks.

## Correct-from-zero shape

- `sources/cover.py` reads the current SEC filing cover and emits exact symbol/class/ratio evidence.
- `EvidenceLoader.identity` selects only the priced common or depositary class.
- `normalize._current_supported_foreign_annual` selects one coherent standard statement namespace
  and reporting currency.
- `normalize._reject_foreign` admits the row only when the current annual, security identity, and
  depositary ratio satisfy the invariant.
- `sync.cover_pages` retains and refreshes the evidence needed by those owners.
- The evidence layer owns later-filing ticker continuity. It reads retained SEC filing bytes and
  emits a dated security-identity observation; normalization consumes the selected observation.
- A separate extraction fallback owns LLM calls and immutable run records. It emits candidates,
  never canonical facts. Existing normalization remains the only owner of statement coherence and
  financial calculations.

Today 237 listed rows still fail: 192 have no exact usable cover title, 27 have a depositary title
without a positive ratio, and 18 lack a coherent standard statement. The current status collapses
the exact exception to `foreign`, so investigation must reproduce the owning exception directly.

Affected decisions: P-02, P-03, P-04, and P-09. Existing SEC/IFRS invariants remain authority.

## Out of scope

- Secondary market-data sites as authority for share ratios or statement currency.
- Showing a row before its evidence contract passes.
- Full source refetch, manual ticker exceptions, pricing guesses, or UI changes during investigation.
- Automatic publication of model-only output, model browsing, or model inference without an exact
  retained-filing citation.
- The 112 `pending_facts` imports; they wait for complete structured statements and are tracked
  separately from unsupported foreign evidence.

## Milestones

| Milestone | State | Owner | Acceptance check |
|---|---|---|---|
| 1. Complete foreign evidence inventory | committed (records commit) | investigation developer | 237 unique rows; 192+27+18 exact; full subclasses and primary samples recorded |
| 2. Generic cover identity parser fixes | committed (`44aed77`) | extraction developer | 100 focused tests; 11 live covers; engine 182; bounded rescan queued |
| 2A. Retain exact cover bytes and support bounded reparse | committed (`44aed77`) | evidence developer | 116 focused; real S3 readback; 100-CIK reparse retained 226 reports and recovered 30 rows |
| 2B. Direct scalar depositary ratio fixes | committed (`44aed77`) | extraction developer | 19 official recoveries, 9 exclusions; 150 focused; real S3 primary readback; engine 183 |
| 2C. Attached-rights regression fix | committed (`44aed77`) | bug-fix developer | exact three restored; adverse classes rejected; 49 focused and 780 full Python tests; engine 184 |
| 3A. Later-SEC same-class ticker continuity | committed (`7570d8b`), gate passed | identity developer | 219 focused; 797 full Python; BRNX/ZTG real `ok` snapshots; offline restart-safe; ambiguous classes and OTC aliases excluded |
| 3A-G. Engine-185 full payload and filing gate | accepted | verification developer | 6,981 unique rows; +BRNX/+ZTG only; 6,979-row regression clean; 279/3,411/378 audits zero wrong |
| 3B. LLM shadow extraction contract and exact prompt | prompt approved; four-model pilot decision pending | extraction developer | provider research recommends Luna, GLM-5.3 Flash, Gemini 3.5 Flash-Lite, and Sonnet 5; no model called |
| 3B-H. Audited pilot request harness and cost bound | accepted; spend approval pending | evaluation developer | 48 exact screen requests; provider configs documented; all local checks pass; no model called; conservative ceiling $16 |
| 3B-S. Four-provider connectivity screen | accepted | evaluation developer | stopped safely after 19 definitive attempts; USD 5.08612760; exact causes recorded; no retry or publication |
| 3B-D. Raw SEC inline-XBRL/exhibit recovery audit | accepted | extraction investigator | 17 standard inline-XBRL plus CNI incorporated exhibit; 18/18 reconcile; zero current LLM candidates |
| 3B-X1. Generic SEC Inline-XBRL supplement parser | accepted | extraction developer | 14 focused; 811 full Python; 17 real filings, 18,775 facts, 124 audited anchors reproduced |
| 3C. LLM shadow pilot for incomplete statements | queued after 3B | extraction developer | retained real filings; candidates stored but dashboard unchanged; failures named |
| 4. Full derive/export/regression/audit | committed (records commit) | manager | engine 184; 6,979 rows; 112 intended disclosure changes; audits zero wrong |

The owner authorized local commits. Push and merge remain unauthorized.

## Verification

- Read-only SQLite/cache inventory with exact ticker lists and current accessions.
- Official SEC filing cover and primary-document evidence for representative and boundary cases.
- Reproduction through `EvidenceLoader` and `build_snapshot`, recording the exact exception.
- Each implementation milestone names focused unit tests and affected real-company checks before it
  starts. Engine/extraction edits require full regression, export, audit, and filing audit.
- A parser repair may re-read an official cover only after exact response bytes are retained and the
  reparse is explicitly bounded; default cover jobs continue to skip immutable covered accessions.
