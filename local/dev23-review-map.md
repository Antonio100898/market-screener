# Developer 23 — review map

Developer state: **IDLE — stopped at first divergence**
Current item: **Engine-187 bounded activation and UI gate**
Review state: **FAILED GATE RECORDED — rerun required**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**BLOCKED — first divergence at bounded acquisition.** I stopped before regress, derive, export,
audit, and audit-filings.

Baseline preserved at `/tmp/market-screener-engine187.zVa9RR/dashboard-engine185.json`: engine 185,
6,981 rows, 6,981 unique tickers, 6,981 unique CIKs, 220,362,103 bytes, SHA-256
`d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`. It remains because the
comparison did not finish.

The exact 18-CIK `python -m screener.sync inline` command returned:

- `0000016868`: `error`.
- `0001304409`: `activated`, `0001683168-26-002801`.
- `0001468642`: `activated`, `0001171843-26-002783`.
- `0001561861`: `activated`, `0001193125-26-197494`.
- `0001841644`: `activated`, `0001213900-26-049950`.
- `0001875016`: `activated`, `0001493152-26-019802`.
- `0001926293`: `unsupported_relationship`.
- `0002000756`: `unsupported_relationship`.
- `0002020932`: `activated`, `0001104659-26-047872`.
- `0002025774`: `activated`, `0001493152-26-036364`.
- `0002039072`: `activated`, `0001683168-26-003530`.
- `0002039972`: `activated`, `0001104659-26-108373`.
- `0002054507`: `unsupported_relationship`.
- `0002058601`: `activated`, `0001213900-26-050426`.
- `0002058897`: `activated`, `0002058897-26-000125`.
- `0002080073`: `activated`, `0001104659-26-052847`.
- `0002080845`: `activated`, `0001193125-26-289382`.
- `0002087398`: `activated`, `0001104659-26-049527`.

First divergence: CNI's 40-F link names `cni-20251231.htm`, while SEC submissions names
`cni-20251231_d2.htm` as the incorporated 6-K's primary document. The acquisition path requires
an exact match and raises `incorporated SEC document does not match submissions`. The accepted CNI
statement is an official SEC-filed exhibit, not that 6-K primary document. CNI has no active
manifest.

Partial runtime mutation: 14 manifests were activated and the same 14 snapshots were marked dirty.
All 18 stored snapshots remain engine 185. The engine-185 UI payload was not replaced. Free disk
was 30 GiB before acquisition and 31 GiB at stop, well above the 1 GiB floor.

Evidence: `local/raw-sec-engine187-e2e-2026-09-28/README.md` and raw logs `00`, `01`, `02` there.
No production code, tests, plans, owner decisions, prompts, or credentials changed. No later gate
ran. I observed no other session touch the owned evidence or review-map paths while this ran.

## Reviewer response

### Review 1 — failed gate evidence accepted

The stop is correct and the evidence is accepted. Engine-185 baseline identity is preserved; the
bounded job activated 14 manifests, left CNI inactive, and ran no later gate. Root cause was the
source-filing primary-document assumption, fixed in `6d97643`. This does not accept engine 187;
the full gate must restart from the preserved baseline and current partial runtime state.
