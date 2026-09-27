# Committed code and storage inventory

State: complete for review. This inventory describes committed application code at
`697ef24750ea5a40f545073498168dc33070ef43` on branch
`codex/market-screener-platform`.

## Result

The current application is one financial engine around local retained files,
SQLite, an exported `dashboard.json`, FastAPI, and a Vite/React client. PostgreSQL
and object storage should extend these owners. They should not create a second
normalizer, screening engine, price pass, or payload contract.

The affected product decisions are P-02, P-03, P-04, P-05, P-07, P-09, P-10,
and P-14. The proposed first slice preserves them by adding a verified immutable
artifact boundary before any current read path changes. See
`reuse-decision.md`.

## Artifacts

- `code-state.json`: worktree, Git, runtime, manifest, command, and machine state.
- `flow.md`: current callers, reads, writes, state changes, transactions, and tests.
- `reuse-decision.md`: what to reuse, what must be isolated, and the first coherent
  PostgreSQL/object-storage slice.

## Material limits

- The shared runtime database, source cache, and UI payload were not opened.
- No tests, services, databases, network calls, installs, or dependency changes ran.
- Runtime data, service behavior, and test results remain `UNVERIFIED`.
- The worktree was already dirty in planning and product documents. Application
  paths under `api/`, `web/`, and `Makefile` had no changes when inventory began.
- GNU Make is unavailable, WSL has no installed distribution, and this worktree
  has neither `api/.venv` nor `web/node_modules`.

## Validation

The developer ran these read-only checks after writing the artifacts:

```powershell
Get-Content -Raw local/worktree-bootstrap-2026-09-25/code-state.json |
  ConvertFrom-Json | Out-Null

git diff --check -- local/worktree-bootstrap-2026-09-25 local/dev1-review-map.md

# Extract each path::symbol citation in the Markdown artifacts. Confirm the file
# exists and the named symbol occurs in it.
$docs = Get-ChildItem local/worktree-bootstrap-2026-09-25 -Filter *.md
$citations = foreach ($doc in $docs) {
  [regex]::Matches((Get-Content -Raw $doc),
    '(?<path>(?:api|web)/[A-Za-z0-9_./-]+)::(?<symbol>[A-Za-z_][A-Za-z0-9_]*)')
}
$missing = foreach ($citation in $citations) {
  $path = $citation.Groups['path'].Value
  $symbol = $citation.Groups['symbol'].Value
  if (-not (Test-Path $path) -or
      -not (Select-String -Path $path -Pattern "\b$([regex]::Escape($symbol))\b" -Quiet)) {
    "${path}::${symbol}"
  }
}
if ($missing) { throw "Missing citations: $($missing -join ', ')" }
```

The commands and observed results are also recorded in the developer return map.
