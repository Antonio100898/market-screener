# Reuse decision

## Product decisions

This increment preserves P-02, P-03, P-04, and P-09. It extracts one shared set of official
facts from retained SEC files, keeps source identity and hashes, and does not select current facts
or publish company data.

## Existing owners

- `api/screener/sources/dera.py::harvest` owns the existing Company-Facts-shaped SEC supplement
  contract and dimension string used by normalization.
- `api/screener/evidence.py::EvidenceLoader` owns evidence assembly. This increment does not edit
  it.
- `api/screener/normalize.py` owns IFRS concept mapping, annual fact selection, statement admission,
  duplicate period policy, and provenance construction.
- `api/screener/sync.py::_derive_evidence` owns snapshot derivation.

## Decision

Add one pure source parser. Reuse the existing `facts -> namespace -> concept -> units -> entries`
shape and existing normalization. Do not create canonical concepts or select dashboard facts here.

A new parser is required because Company Facts omits current annual facts and DERA quarterly files
arrive later. Neither existing source reads a retained Inline-XBRL instance, its primary-document
anchors, or its presentation roles.

The parser uses only the Python standard library. It requires explicit metadata, paths, and expected
SHA-256 hashes. It fails before emitting facts when retained evidence is missing or inconsistent.
