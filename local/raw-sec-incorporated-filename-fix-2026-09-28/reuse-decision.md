# Reuse decision

Affected decisions: P-02, P-03, P-04, and P-09.

`sync._retain_inline_annual` owns retained SEC acquisition. It already reads the
annual relationship, submissions metadata, source filing index, and all parser
inputs. `inline_xbrl.verify_manifest` owns the retained provenance contract.
`EvidenceLoader` and normalization consume that verified manifest.

Reuse the existing flow. Keep accession, form, and filing date from submissions.
Use the annual relationship's exact document as the source statement only after
`_inline_documents` confirms that the source accession index contains it. Preserve
the submissions primary document separately as filing-wrapper metadata. No second
resolver or company-specific rule is needed.
