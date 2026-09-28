# Fix register

- Foreign rows missing on this Mac: current SEC cover identity was absent from local SQLite, so
  valid 20-F facts were classified `foreign`. Kind: runtime evidence gap. Owner: generic cover
  scan and derivation. Status: fixed for evidence-supported rows. Verification: the full scan read 1,234 foreign/pending covers; engine-181
  derive moved 912 listed rows to `ok`, including NTES, and left no dirty snapshots.
- Remaining foreign exclusions: 192 lack an exact cover title for the mapped ticker, 27 have a
  depositary title without a resolved positive ratio, and 18 lack a coherent standard statement
  in one identifiable currency. Kind: evidence/support boundary. Status: open; do not guess.
- PostgreSQL snapshot contract Review 1: annual cover/security-basis evidence was frozen as stable
  identity, and artifact links could grow on an old snapshot. Kind: data-history contract defect.
  Owner: persistence milestone. Status: fixed. Verification: later-cover and changed-artifact regression cases.
- Bulk import Review 1: 501 stored cover rows lacked title/accession but bypassed the verified
  ticker-map fallback; 485 have an exact map match. Kind: evidence-routing defect. Owner: bulk
  importer. Status: fixed. Verification: incomplete-cover match/mismatch tests and reconciled bulk rerun.
- Bulk import Review 2: all 485 recovered rows dropped their incomplete retained `receipt` object
  from the canonical payload, creating provenance-only hash mismatches. Kind: payload parity defect.
  Owner: bulk importer. Status: fixed. Verification: 6,655 exact SQLite hashes, zero mismatches.
- Engine-183 regression: plural `rights?` classification rejected CTRM, CVE, and SOBO common shares
  with attached purchase rights. Kind: cover-class regression. Owner: cover predicate. Status:
  fixed in engine 184. Verification: exact three-title pins, adverse rights/warrant controls, full regression.
- Engine-187 activation: incorporated statement link was incorrectly required to equal the source
  filing's `primaryDocument`, rejecting official SEC exhibits such as CNI's linked audited statement.
  Kind: source-relationship defect. Owner: incorporated SEC acquisition. Status: fixed. Verification:
  failing-before regression, 131 focused tests, and real CNI activation/derivation with separate
  6-K wrapper and exact exhibit identities.
- Engine-187 activation: incorporation prose was inspected before the annual filing's own structured
  instance. Three valid direct 20-Fs were rejected by unrelated incorporation language. Kind:
  source-routing defect. Owner: SEC annual acquisition. Status: fixed. Verification: failing-before
  regression, 133 focused tests, three retained direct activations, and preserved CNI fallback.
