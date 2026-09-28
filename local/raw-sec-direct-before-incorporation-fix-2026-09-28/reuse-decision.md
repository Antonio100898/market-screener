# Reuse decision

Affected decisions: P-02, P-03, P-04, P-05, and P-09.

`sync._retain_inline_annual` owns SEC annual source routing and is called by
`retain_inline_statements`. `_inline_documents` already validates a direct filing's
structured files. `inline_xbrl.incorporated_annual_source` already validates the
fallback relationship.

A correctly built flow asks the annual index for a complete direct instance first,
emits a direct manifest when present, and consults filing prose only when the
extracted instance is absent. The current flow reverses those checks.

Reuse both existing validators. Move the direct check before relationship discovery.
Do not add a company rule, parser, or second routing owner.
