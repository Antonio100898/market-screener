# Engine 187 activation gate

**Result: BLOCKED at the bounded acquisition gate.** CNI did not activate, so no regression,
derive, export, or audit command ran.

The source-relationship defect was later fixed in `6d97643`. This directory remains the evidence
for the failed first gate; a separate directory records the required clean rerun.

## Baseline

- Preserved outside the repository at
  `/tmp/market-screener-engine187.zVa9RR/dashboard-engine185.json`.
- SHA-256: `d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`.
- Size: 220,362,103 bytes.
- Engine: 185.
- Rows: 6,981.
- Unique tickers: 6,981.
- Unique CIKs: 6,981.
- The baseline remains because the blocked comparison never finished.

## Bounded acquisition result

The one approved command received exactly the 18 named CIKs. It returned 14 `activated`, three
`unsupported_relationship`, and one `error`:

| CIK | State | Accession when activated |
|---|---|---|
| `0000016868` | `error` | — |
| `0001304409` | `activated` | `0001683168-26-002801` |
| `0001468642` | `activated` | `0001171843-26-002783` |
| `0001561861` | `activated` | `0001193125-26-197494` |
| `0001841644` | `activated` | `0001213900-26-049950` |
| `0001875016` | `activated` | `0001493152-26-019802` |
| `0001926293` | `unsupported_relationship` | — |
| `0002000756` | `unsupported_relationship` | — |
| `0002020932` | `activated` | `0001104659-26-047872` |
| `0002025774` | `activated` | `0001493152-26-036364` |
| `0002039072` | `activated` | `0001683168-26-003530` |
| `0002039972` | `activated` | `0001104659-26-108373` |
| `0002054507` | `unsupported_relationship` | — |
| `0002058601` | `activated` | `0001213900-26-050426` |
| `0002058897` | `activated` | `0002058897-26-000125` |
| `0002080073` | `activated` | `0001104659-26-052847` |
| `0002080845` | `activated` | `0001193125-26-289382` |
| `0002087398` | `activated` | `0001104659-26-049527` |

## First divergence

CNI (`0000016868`) failed before its source filing was retained. The 40-F relationship points to
`cni-20251231.htm`. SEC submissions identifies `cni-20251231_d2.htm` as the 6-K primary document.
`_retain_inline_annual()` requires those names to be equal and raises
`incorporated SEC document does not match submissions`.

This conflicts with the accepted CNI contract. The linked audited statement can be an official
SEC-filed exhibit without being the source filing's primary document.

## Runtime state at stop

- Fourteen current manifests were created.
- The same 14 snapshot rows were marked dirty.
- CNI has no current manifest and was not marked dirty.
- Dashboard remains the engine-185 baseline; it was not replaced.
- Free disk was 30 GiB before acquisition and 31 GiB when stopped.
- No code, tests, plans, decisions, prompts, or credentials changed.

Raw logs:

- `00-baseline-and-resources.log`
- `01-inline-activation.log`
- `02-stop-resources.log`
