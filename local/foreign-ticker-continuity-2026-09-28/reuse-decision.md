# Reuse decision

- `cover.securities` owns filed class/symbol parsing. Extend `cover` with one narrow reader for an
  explicit later-filing class/symbol/exchange statement; do not add an alias table.
- `store.set_cover`, `cover_for`, and `covers_by_cik` own cover persistence and its callers in
  `EvidenceLoader.identity`, `_index_tickers`, `cover_pages`, derive, and export. Keep that current
  contract and add immutable observations plus a continuity activation operation.
- `EvidenceLoader` owns security selection. It will accept a later symbol only when one retained
  annual class is named exactly in the later SEC filing and that filing explicitly lists the same
  class on the current exchange under the current ticker.
- `cover_pages` already retains exact response bytes before parsing and reads SEC submissions.
  Reuse those paths for bounded later-filing reads.
