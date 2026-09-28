# Owner decisions

1. Continue the new architecture on this Mac. Different local baselines do not permit different
   calculations for the same retained filings and security evidence.
2. Foreign tickers use the general supported-foreign flow. Do not make an NTES-only rebuild.
3. Run the work through managed delivery with written milestones, delegated implementation, and
   review gates.
4. Restart OrbStack to recover the local PostgreSQL and S3 verification services.
5. Commit the accepted work locally on `codex/company-import`. Do not push or merge it.
6. A later official SEC filing may prove that the same security class changed ticker. Retain the
   old annual-cover evidence and the later change evidence, keep one stable security identity, and
   activate the new ticker only when issuer, class, and exchange are explicit. Do not infer ticker
   changes from punctuation or unsupported OTC aliases.
7. Add an LLM fallback after deterministic extraction fails. The model may emit evidence-citing
   candidates only. Store the filing, cited location, model identity, prompt revision, response,
   and checks. Publish a candidate only after deterministic verification; otherwise fail closed.
   Start in shadow mode on the incomplete foreign-company set. The exact model prompt still needs
   owner approval before implementation.
8. Approve the exact statement-extraction prompt in
   `local/llm-statement-extraction-proposal-2026-09-28/prompt.md`. The first implementation remains
   supplement-only and shadow-only: it can propose deterministically missing fields but cannot
   override deterministic data or change the dashboard.
