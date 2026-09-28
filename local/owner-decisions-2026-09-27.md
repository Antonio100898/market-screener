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
