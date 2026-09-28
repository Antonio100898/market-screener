# Direct depositary ratios

Affected decisions: P-02, P-03, P-04, P-09, and P-17.

## Result

The fixed-accession SEC check has 28 current ratio candidates. Nineteen become exact direct
underlying-shares-per-receipt ratios. Nine stay excluded.

Recovered:

The 19 recovered rows are CDLR, ERIC, GMAB, GOTU, HDB, ING, JG, NCNA,
NVO, POM, PSNY, RELX, RERE, SNY, SOGP, SSL, SUZ, SY, and WKEY.

The accepted report expected 20 direct recoveries within its original 27-row ratio inventory.
Official full-primary-document evidence reduces that set to 18: ADAG contains several unequal
ADS/share counts, and FMS contains both its historical 1:1 relation and current 2:1 relation. The
parser returns null instead of choosing one. SUZ is an additional current engine-182 foreign row
whose direct 1:1 relation is recovered by the same generic parser, producing 19 current recoveries.

Adverse and unresolved rows:

- ADAG and FMS: multiple different direct relations.
- FMX and KOF: compound units.
- VLRS: ADS-to-CPO-to-share chain.
- WAVE: conflicting current and historical counts.
- BBD: preferred-share relation.
- BMA and WDS: no unique direct filing-backed relation.

`real-ratio-output.json` records each fixed SEC URL, accession, response size, SHA-256, exact title
where applicable, and result. Run it with:

```sh
PYTHONPATH=api api/.venv/bin/python \
  local/foreign-ratio-parser-2026-09-28/verify_real_ratios.py
```

Observed: `28 cases, 19 direct recoveries`.

## Retained evidence

Primary documents are cached at
`covers/<accession-without-dashes>/primary/<SEC-primary-document>`. Sync reads that immutable path
first, retains a successful source response atomically before parsing, and consumes it only for one
unresolved depositary class matching the requested ticker. The importer verifies the cached
document produces the stored ratio before linking it as `raw_cover_primary_document`.

The real local S3/PostgreSQL integration read back the 61-byte NTES primary fixture from
`raw/sha256/24e991ef0f209d06357df4514bf82c76814f6509fe44d617fbfd042a2a4a7d68`.
The four-company retry retained 13 artifacts and 13 snapshot links. ABT, 6752.T, and CATO payload
hashes remained unchanged; NTES kept accession `0001104659-26-043468` and exact ratio `5`.

## Manager command

This is the exact bounded 28-CIK current ratio set. It runs only cover reparse. The manager owns
derive, export, regression, audit, and filing audit after review.

```sh
PYTHONPATH=api api/.venv/bin/python - <<'PY'
from screener import store, sync

ciks = {
    "0000314590", "0000353278", "0000717826", "0000844551", "0000909327",
    "0000910631", "0000929869", "0001039765", "0001061736", "0001121404",
    "0001144967", "0001160330", "0001333141", "0001347426", "0001434265",
    "0001520504", "0001709626", "0001737339", "0001738699", "0001758530",
    "0001768259", "0001783407", "0001818838", "0001838957", "0001846715",
    "0001877971", "0001884082", "0001978867",
}
assert len(ciks) == 28
connection = store.connect()
sync.cover_pages(
    connection,
    foreign_only=True,
    ciks=ciks,
    reparse=True,
)
PY
```

## Checks

- Fixed SEC accessions: 28 cases, 19 direct recoveries.
- Focused cover/sync/import checks: 150 passed, 1 storage test skipped in the non-storage run.
- Real S3/PostgreSQL import and primary-document readback: 1 passed.
- Full suite: 780 Python passed, 6 skipped, 1 dependency warning; 113 web passed.
- `compileall` and `git diff --check`: passed with no output.
- Live ratio reparse and full derive/export payload gates: intentionally not run.
