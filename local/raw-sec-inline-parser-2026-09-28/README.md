# Generic SEC Inline-XBRL parser evidence

The pure parser reproduced all 124 accepted statement anchors from the 17 direct SEC filings. It
emitted 18,775 standard IFRS or US-GAAP facts after deterministic duplicate handling.

Every reproduced anchor kept the accepted value, unit, period, decimals, Inline-XBRL scale/sign,
statement roles, source accession/document, annual relationship, and retained-file hashes. No SEC
bytes are stored in Git.

Run:

```sh
PYTHONPATH=api python3 local/raw-sec-inline-parser-2026-09-28/verify_parser.py
```

Expected output:

```json
{"all_selected_anchors_reproduced": true, "all_source_hashes_preserved": true, "companies": 17, "selected_anchors": 124, "standard_facts": 18775}
```

Per-company counts and checked anchors are in `results.json`. CNI is excluded because incorporated
exhibit discovery is a later increment.
