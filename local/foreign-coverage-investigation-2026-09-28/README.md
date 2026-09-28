# Foreign evidence inventory

State: working. The retained store reproduces all 237 listed `foreign` snapshots.

## Reconciled top-level result

- 192: `foreign filer has no exact filing-cover security title for this ticker`
- 27: `foreign depositary security has no resolved positive cover-page ratio`
- 18: `filer's current foreign annual report does not carry a coherent standard US-GAAP or IFRS balance sheet in one identifiable ISO currency`
- Total: 237

The set includes four listed preferred tickers still on engine 178. The other 233 rows are on
engine 181. This explains why a dashboard-eligible query that excludes preferred tickers returns
233 while the requested listed inventory returns 237.

## Trace and reuse decision

The investigation reads `~/.cache/graham-screener/screener.db` in SQLite read-only mode and loads
the retained `companyfacts_<CIK>.json` files. `EvidenceLoader.identity` owns exact ticker-to-cover
selection and retained-title ratio reparsing. `normalize.build_snapshot` calls
`normalize._reject_foreign`, which owns the three reproduced exceptions. The report will reuse
these owners. It does not propose a second eligibility path.

Correct-from-zero flow: `sources/cover.py` extracts exact class and ratio evidence;
`EvidenceLoader.identity` selects the priced class; `_current_supported_foreign_annual` selects one
standard statement and currency; `_reject_foreign` admits only a coherent statement, exact current
class, and positive depositary ratio.

Affected decisions are P-02, P-03, P-04, and P-09. This read-only increment preserves all four and
the foreign-filer invariant. No unsupported row is admitted.

## Evidence status

All 237 rows were reproduced from retained evidence. `inventory.json` contains the full cover rows,
current filing detail, exception, subclass, and source location. `inventory.csv` is the flat index.

Command:

```sh
PYTHONPATH=api api/.venv/bin/python \
  local/foreign-coverage-investigation-2026-09-28/build_inventory.py
```

Observed output:

```text
rows=237 cover_identity=192 depositary_ratio=27 statement_coherence=18
```

## Cover identity subclasses: 192

The subclasses are about the retained evidence. They do not claim that a row is safe to admit.

### Empty or parser failure: 47

AURE, AZN, AZUL, BBVA, BEP, BSAC, CCJ, CLWT, CNEY, CRESY, DAVA, DOX, DPU, DSGX,
DSX, EHLD, EMA, EPWKF, FAMI, FURY, GGAL, GLAS, GLBS, GNS, ICON, IMMP, IRS, KAZR,
LKNCY, MCRP, MOGU, MUFG, NA, PAVS, PLTYF, PSHG, RBNE, SAN, SGRX, SUZ, SXTC, TANH,
TORO, USAS, USEA, VOXR, ZCMD.

Observed shapes include an empty exact title, boolean `true`/`F` captured as the symbol, a compound
symbol cell, and valid common titles rejected because the words `right` or `preferred stock purchase
rights` trip the non-common regex. The current BBVA 20-F is the boundary example: its exact title is
an ADS with the right to receive one ordinary share, not a warrant or right.

### Symbol mismatch: 24

AKO-A, ATTT, BRNX, BVC, EOCN, GMEX, GRSD, HELP, HERE, LKFT, LYG, MF, NIKI, OGG,
ORIO, PBK, PLGO, QTEX, SLMT, SVRN, UZX, VIVO, YFOR, ZTG.

These rows have retained cover classes, but none uses the mapped ticker exactly. Some are punctuation
differences, such as `AKO-A` versus filed `AKO.A`. Most are newer market symbols paired with an older
annual-cover symbol. A broad alias rule would guess across security identities.

### Wrong security class: 11

ATMP, BIPH, BPYPP, CEF, CIG, GGB, GLDI, ITUB, PHYS, PSLV, SPPP.

The exact filed class is an ETN, debt, preferred equity, or trust/fund unit. These are not recovery
targets. ATMP's current 20-F identifies the exact ticker as an iPath Select MLP ETN.

### Stale or OTC mapping: 46

ABLZF, AMBIQ, AMLIF, ATEYY, BETRF, BGICF, BHATF, BRCNF, CAJPY, CASIF, CHKIF,
CILJF, CKDXF, CPTAF, EGLXF, FNCTF, FTRKF, GRTUF, GVHGF, HTHIY, ILLMF, KYOCF,
MAXNQ, MDNAF, MMTZF, NMPGY, NTTYY, ORISF, PREJF, PYRGF, REEAF, RMTHF, SELXF,
SLAIY, SNNRF, SPTJF, TARSF, TCGLF, TEFOF, TIRXF, TNCAF, UOKAF, VQSSF, WILCF,
YGMZF, ZTEKF.

The mapped ticker is an OTC-style symbol or otherwise differs from the registered class. For CAJPY,
the retained and official 20-F cover registers `CAJ`, not `CAJPY`. These rows need a supported OTC
identity policy or must stop being treated as exact registered classes.

### No registered 12(b) class: 7

AGRZ, ALMMF, ARRKF, ASAIY, LYTHF, RAJAF, SNPMF.

The retained cover value is `N/A` or `None`. ARRKF's official 20-F leaves the 12(b) table empty and
lists common shares only under Section 12(g).

### Missing retained cover evidence: 57

AHL-PD, AIJTY, ALRTF, ALYAF, AMUB, AVCRF, AVLNF, BRQSF, CRLBF, CSCIF, CWLXF,
DAZSD, EGG, EGMCF, EHVVF, ERLFF, FFMGF, FRFHF, GDRZF, GEBRF, GIGGF, GLOP-PA,
GNOLF, GNTOF, GRVT, HAMVF, HMELF, HRNNF, ICTSF, ITMSF, KDOZF, KIQSF, LINMF,
LVRLF, MMTIF, MTLK, NSFDF, NYXH, PCCYF, PHOS, QZMRF, RLNDF, RTCJF, SEAL-PA,
SGBAF, SPOWF, STNDF, TAC, TANAF, TCPA, TGB, TLIH, TRTN-PA, UBS, WEBNF, XTGRF,
XTXXF.

There is no `security_cover` row for these CIKs. That is the complete local finding; it does not
collapse fetch failure and no class into one claimed cause. UBS is the official sample: its current
40-F sends the class list to page 3, while the retained store has no cover row.

## Depositary ratio subclasses: 27

### Ratio is in the current cover title: 18

ADAG, CDLR, ERIC, FMX, GMAB, GOTU, HDB, JG, KOF, NCNA, NVO, POM, RELX, RERE,
SNY, SOGP, SY, WKEY.

The current grammar misses parenthesized digits, fractional words, reverse ratios, B/equity shares,
and units. Official examples: CDLR says each ADS represents four ordinary shares; JG says every
three ADSs represent 40 common shares; SNY says each ADS represents one half of one ordinary share.

### Ratio is in current primary-document prose: 6

FMS, ING, PSNY, SSL, VLRS, WAVE.

Official examples: FMS states that two ADSs represent one share; VLRS states one ADS represents ten
CPOs and each CPO represents one Series A share; WAVE contains current and historical ADS/share
relations in transaction notes. WAVE must not use the first numeric relation found in the document.

### Tagged DEI ratio fact: 0

No separate standard DEI numeric ratio fact appears in retained Company Facts for these 27 rows.
The ratio-bearing `Security12bTitle` text is already counted under the cover-title group.

### Wrong-class pairing: 1

BBD. Its current cover says ticker BBD is an ADS representing one preferred share. Preserving the
full title should move it to the existing wrong-security-class rejection, not admit it.

### No current filing-backed ratio found: 2

BMA, WDS. Their current primary documents name ADSs but do not state a numeric relation. BMA's
incorporated 2006 F-6 template also omits the number. No ratio may be assumed.

## Statement coherence subclass: 18

### Incomplete Company Facts: 18

AERO, AGMB, AHNRF, ALPS, AUGO, BRBI, CIB, CNI, DAVI, GCDT, GMTL, HBNB, NXAT,
PAYP, PICS, TMCR, VMET, YMAT.

Every current accession has zero standard US-GAAP or IFRS balance anchors. Seventeen expose only
one to three current numeric facts; DAVI exposes four. None has a currency tie to evaluate because
there is no standard balance. Counts for mixed-currency, extension-only standard facts, and an
identifiable unsupported standard basis are all zero.

Official samples confirm the boundary, not support: AERO's current 20-F has a coherent 10:1 ADS
cover but retained Company Facts has one current DEI share fact; CNI's 40-F primary document does
not provide a Company Facts standard balance; PICS has an exact common-share cover while retained
Company Facts has only current SRT data. These rows need complete structured statements, not a
weaker normalization gate.

## Recommended fix families

1. **Preserve complete cover pairs.** Owner: `sources/cover.py::_table_securities`, `securities`,
   `unique_securities`, and the common-equity predicates. This is generic because it repairs table
   pairing, placeholder symbols, and title classification before any ticker-specific decision.
   Expected effect: 47 rows move past the missing-title gate; some may then fail ratio or another
   valid gate. Prove with BBVA, BEP, AZN, and SUZ plus HON, PPG, LX, and TM neighbours.
2. **Record cover fetch outcomes.** Owner: `sync.cover_pages.read` and the retained cover evidence
   contract. Store which R report or primary document was tried and the exact terminal error. This
   is generic observability, not an admission rule. Expected recoverable count is unknown across the
   57 missing rows until a bounded rescan. Prove with UBS, TGB, NYXH, and one no-class neighbour.
3. **Extend ratio grammar only for direct scalar relations.** Owner:
   `sources/cover.py::depositary_ratio`, with primary-document fallback in `sync.cover_pages.read`.
   Parentheses, fractions, `every N`, and direct B/equity/common-share wording are generic. Twenty
   rows have safe direct relations: 16 cover-title rows excluding FMX/KOF, plus FMS, ING, PSNY, and
   SSL. Prove with CDLR, GMAB, JG, SNY, FMS, and the existing AMBO, NTES, OPT, and TM neighbours.
4. **Keep class-policy exclusions.** Owner: `EvidenceLoader.identity` and
   `normalize._reject_foreign`. The 11 wrong classes, seven unregistered classes, and BBD should
   remain excluded. Expected recovered count: zero. Tests should prove the improved parser exposes
   the true rejection instead of accidentally admitting them.
5. **Do not alias changed symbols.** Owner: security identity ingestion, not normalization. The 24
   symbol mismatches and 46 stale/OTC mappings need current filing-backed identity or an owner-approved
   OTC/post-annual-symbol policy. A narrow dot/hyphen canonical rule could recover AKO-A, but even
   that changes the stated exact-match rule and needs owner approval. Expected generic recovery now:
   zero.
6. **Treat non-standard statements as source adapters.** Owner:
   `normalize._current_supported_foreign_annual` remains the gate; source-specific import owns any
   new evidence. All 18 statement rows need complete official statements in one currency. Expected
   generic Company Facts recovery now: zero. Prove any adapter with its real current filing, a prior
   filing, currency checks, and a standard US-GAAP/IFRS neighbour.

## Adapter or owner-decision rows

- FMX and KOF use compound units rather than direct shares. VLRS uses a CPO chain. WAVE contains
  multiple historical/current relations. These need a filing-specific basis review before deciding
  whether a generic resolver is safe or a company adapter is required.
- BMA and WDS need new official ratio evidence or remain unsupported.
- The 18 statement rows need official-source adapters or later complete SEC structured facts.
- The 24 symbol mismatches and 46 stale/OTC mappings require an identity-policy decision; they must
  not be repaired with ticker aliases in normalization.

## Recommended milestone split

1. Cover parser and retained fetch-outcome evidence: the 47 parser rows and 57 missing-evidence rows.
2. Direct scalar depositary ratios: the 20 safe rows, with BBD as an adverse class control.
3. Security identity decisions: symbol mismatches, OTC mappings, and compound-unit receipts.
4. Statement adapters: one approved source family or company at a time for the 18 rows.
5. Full derive/export/regression/audit only after accepted implementation increments.

## Official examples inspected

- Parser: BBVA 2026 20-F,
  `https://www.sec.gov/Archives/edgar/data/842180/000162828026010001/bbva-20251231.htm`.
- Symbol mismatch: AKO-A 2026 20-F,
  `https://www.sec.gov/Archives/edgar/data/925261/000110465926038506/akoa-20251231x20f.htm`.
- Wrong class: ATMP 2026 20-F,
  `https://www.sec.gov/Archives/edgar/data/312070/000031207026000006/bbplc-20251231.htm`.
- Stale/OTC: CAJPY 2023 20-F,
  `https://www.sec.gov/Archives/edgar/data/16988/000119312523084415/d394490d20f.htm`.
- No registered class: ARRKF 2024 20-F,
  `https://www.sec.gov/Archives/edgar/data/1855743/000107997324000310/arras-20231031.htm`.
- Missing local cover: UBS 2026 40-F,
  `https://www.sec.gov/Archives/edgar/data/1114446/000161052026000026/ubs-20251231.htm`.
- Statement boundary: AERO, CNI, and PICS current filings, whose URLs are recorded in
  `inventory.json` through their accessions and Company Facts source locations.
- Ratio examples are limited to the three named in each ratio subsection. Exact primary-document
  URLs for all 27 ratio rows are in `inventory.json`.

## Limits

- The 57 rows with no retained cover were not refetched as a batch. The official check is bounded to
  UBS. Their local subclass is therefore exactly "missing retained cover evidence"; the external
  cause remains unverified.
- The investigation did not import exhibits or construct company adapters for the 18 statement rows.
- No derive, export, regression, or audit was run because this increment changes no engine,
  extraction, evidence, pricing, profile, or serialization behavior.
