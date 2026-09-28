# Attached purchase-rights reuse decision

Affected decisions: P-02, P-03, P-04, and P-09. The foreign-filer security gate remains unchanged.

`cover.is_common_equity_security` owns the common-class decision. Its callers in `sync.py`,
`evidence.py`, and `normalize.py` already route every cover title through this predicate.

The existing predicate first removes recognized explanatory text, then applies the shared
non-common token check. Extend that same normalization step to remove only a trailing attached
purchase-rights clause after a named common or ordinary share class. Do not add a caller guard,
ticker list, identity alias, or second classification path.

Correct flow: the filed title stays unchanged as evidence; the cover predicate ignores only the
attached-rights suffix for classification; the normal non-common check still rejects standalone
rights, warrants, preferred classes, notes, ETNs, and debt.
