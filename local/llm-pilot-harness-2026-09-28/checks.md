# Dry-run checks

State: **READY_FOR_REVIEW — model behavior remains UNVERIFIED**.

## Observed checks

```text
python3 -m py_compile local/llm-pilot-harness-2026-09-28/pilot.py
PASS

python3 local/llm-pilot-harness-2026-09-28/pilot.py self-test
PASS raw_hashes source_maps evidence schema dry_run_no_network secret_scan deterministic_rebuild

git diff --check -- local/llm-pilot-harness-2026-09-28 local/dev17-review-map.md
PASS

secret-pattern grep on owned non-generated files
PASS — no match
```

The self-test rebuilt every artifact in a temporary directory and compared every file hash. It
resolved every emitted source ID against the matching frozen raw hash and character span. It also
confirmed that required statement anchors, fact units, scales, and the CNI exhibit link survive;
the wrapper-only case excludes the exhibit source; found/not-found/ambiguous shapes survive the
common schema transform; invalid shapes fail; and dry-run makes no socket connection.

## Artifact hashes

- Approved prompt file: `4ec071fca4918d3214557ff1a49740aa8c6ed820fdbb2f520dd855c1c2ad2c99`
- Canonical schema: `ff1677190a47687587fd9b313c110e6d4e5bbbf5160d9067fc49db8caeff809b`
- Common provider schema: `ece791b32e1d01c137766f087681fc33ac27497643fae9d9846d1618befd8451`
- Schema-transform record: `a6de4cb2e6c56fbe930581ba69caa63a2a6284d974a2e509e2dc7755203c20c9`
- Ordered 48-request set: `85f81d99e4bade8f9c9c5f409fd67a9c8ca207496380e529ecf7f41cf69cf8e2`
- Run plan: `fe5979ad7eaa4d68d40950d2456552d5ea583f5f337b41f52bd6886387cda4f1`

| Source view | Raw bytes | View bytes | View SHA-256 | Map SHA-256 |
|---|---:|---:|---|---|
| AERO `d101275d20f.htm` | 6,601,960 | 1,417,792 | `7f62515263b9e7308248d32c14954e55e66d61acace2ff7d7a26d8a36c0aeaab` | `c400395e850e3699419246dacd5c0b60d0ae6f69fd482fc4643e8acee52aa108` |
| CIB `cib-20251231.htm` | 14,587,757 | 2,337,038 | `158706106e6594a9ce8ed2045ff34c1217f21f8967f7bf1583dc6cb66be32771` | `423210da102dc4855da2cc3221fa271ee1f78dbc7929d62e44d26b4c7244b0d2` |
| CNI wrapper `tm261145d1_40f.htm` | 420,374 | 98,339 | `6e09e01934ebbda2c463413a5d255653c6601ffa1b45442000cdbb871de2a37a` | `15c1f6d7bf450a3f460a3cf18e2b77620e7afdb93dbbd076bd09bbd8f66df416` |
| CNI Exhibit 99.2 `cni-20251231.htm` | 2,665,133 | 492,441 | `afefab653989689601a6b9ae555706ebe8f39a90fa3ae94cbf04c81be53b3495` | `99b36ab84abcc2670c1b60d0be6ff22128e4c5098b2b793dde59d828de593f54` |

## Remaining limits

- Provider key access and exact model availability are untested.
- The byte-based token bounds are conservative estimates, not provider tokenizer counts.
- Provider schema compilation and response paths are documented and locally shaped, but not yet
  observed from a live response.
- No extraction quality, latency, output-token, or billed-cost result exists.
- S3/PostgreSQL extraction-run persistence remains a later implementation gate.
