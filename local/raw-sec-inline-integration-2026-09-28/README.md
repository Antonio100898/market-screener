# Retained direct annual integration

The production path now retains and activates verified direct 20-F/40-F
Inline-XBRL inputs, then merges only missing Company Facts entries at the shared
evidence boundary. Engine version is 186.

The real verifier used small temporary manifests and read the existing 204 MB
recovery cache in place. It did not copy SEC bytes or mutate the main store.

| Ticker | Before | After | Annual source | Balance date | Remaining failure |
|---|---|---|---|---|---|
| AERO | foreign | ok | 0001193125-26-197494 | 2025-12-31 | — |
| AGMB | foreign | ok | 0001104659-26-047872 | 2025-12-31 | — |
| AHNRF | foreign | foreign | 0001683168-26-002801 | — | No exact filing-cover title |
| ALPS | foreign | ok | 0001493152-26-036364 | 2026-03-31 | — |
| AUGO | foreign | ok | 0001171843-26-002783 | 2025-12-31 | — |
| BRBI | foreign | foreign | 0001213900-26-050426 | — | No exact filing-cover title |
| CIB | foreign | foreign | 0002058897-26-000125 | — | No positive filing-cover receipt ratio |
| DAVI | foreign | ok | 0001683168-26-003530 | 2025-12-31 | — |
| GCDT | foreign | ok | 0001493152-26-038530 | 2026-03-31 | — |
| GMTL | foreign | ok | 0001104659-26-108373 | 2026-06-30 | — |
| HBNB | foreign | ok | 0001213900-26-049771 | 2025-12-31 | — |
| NXAT | foreign | foreign | 0001829126-26-005357 | — | No exact filing-cover title |
| PAYP | foreign | ok | 0001193125-26-289382 | 2026-03-31 | — |
| PICS | foreign | ok | 0001213900-26-049950 | 2025-12-31 | — |
| TMCR | foreign | ok | 0001104659-26-049527 | 2025-12-31 | — |
| VMET | foreign | ok | 0001104659-26-052847 | 2025-12-31 | — |
| YMAT | foreign | ok | 0001493152-26-019802 | 2025-12-31 | — |

All 17 manifests applied. Thirteen rows now derive successfully. The four
remaining failures are separate security-cover evidence gaps, not statement
parser failures.

Run:

```sh
PYTHONPATH=api api/.venv/bin/python \
  local/raw-sec-inline-integration-2026-09-28/verify_integration.py
```

Exact machine-readable results are in `results.json`.
