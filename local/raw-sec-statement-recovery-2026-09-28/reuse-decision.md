# Reuse decision

## Correct owner shape

The SEC source layer owns extraction of machine-readable facts from official SEC filings. It must
supplement a current annual filing when Company Facts omits facts that exist in Inline XBRL. It
must not ask an LLM to re-read standard concepts, infer missing totals, or create company-specific
exceptions.

The supplement should emit the same Company Facts-shaped entries already consumed by
normalization:

`facts -> ifrs-full/us-gaap -> concept -> units -> entries`

Each entry keeps the exact standard concept, value, unit, period, context, annual relationship,
source accession, source document, Inline XBRL fact ID, scale, and sign. CNI also needs both the
incorporating 40-F accession and the source 6-K exhibit accession in provenance.

## Existing owners to reuse

- `api/screener/sources/dera.py::harvest` already reshapes SEC facts into Company Facts entries and
  preserves dimensions. It remains the owner for quarterly SEC data-set supplements; the new path
  must not duplicate its selection policy.
- `api/screener/normalize.py::_ifrs_as_us_gaap` already owns standard IFRS-to-canonical concept
  translation. Raw SEC extraction should preserve `ifrs-full` concepts and call this owner through
  normal derivation.
- `api/screener/normalize.py::_reject_foreign` owns annual statement-basis, currency, and filing
  admission. The supplement must satisfy this gate with verified annual relationships; it must not
  bypass it as a generic canonical adapter.
- `api/screener/evidence.py::EvidenceLoader.load` owns assembly of Company Facts, DERA evidence,
  and cover identity. Add the verified supplement here or immediately before this boundary.
- `api/screener/sync.py::_derive_evidence` remains the only snapshot derivation entry point.
- `api/screener/object_store.py::ImmutableObjectStore.put_verified`,
  `api/screener/artifacts.py::store_evidence`, and
  `api/screener/company_import.py::CompanyImporter._retain` own immutable storage and readback.
- `api/screener/sources/edinet_mapper.py::build_edinet_companyfacts` and
  `api/screener/sources/ifrs_workbook.py::build_adidas_companyfacts` show the canonical adapter
  contract for non-SEC sources. They are useful contract references, not the correct SEC parsing
  owner.

## Smallest generic path

1. Detect that the newest 20-F/40-F has no coherent standard balance in Company Facts.
2. Retain the SEC index, primary document, extracted instance, Filing Summary, and taxonomy files.
3. Follow an incorporated document only when the annual filing explicitly identifies the SEC-filed
   exhibit and exact accession/document.
4. Read standard `ifrs-full` or `us-gaap` facts for the annual report period from undimensioned
   issuer contexts. Use presentation roles to require primary-statement placement.
5. Preserve source facts without arithmetic. Accept a balance only when Assets equals the filed
   combined total, or equals separately filed Liabilities plus Equity.
6. Merge verified missing facts into the standard namespace. Never override an existing
   deterministic fact.
7. Run the existing evidence, normalization, derivation, artifact, and PostgreSQL owners.

The incorporated-exhibit relationship needs explicit provenance. Do not collapse CNI's source 6-K
accession into the 40-F accession or lose the annual 40-F relationship. Extend the provenance
record if the current single accession field cannot express both.

## Rejected paths

- LLM extraction for the audited 18: every row has deterministic standard taxonomy evidence.
- Company-specific parsers: no company-specific format is required.
- Mapping extension tags for these rows: none is needed for a coherent current balance.
- Deriving absent current subtotals for BRBI, CIB, PAYP, or PICS: their filed balances are
  unclassified.
- Writing extracted values directly to snapshots or PostgreSQL: this would bypass normalization,
  historical selection, and provenance.
