# Increment 3 reuse decision

Affected decisions: P-02, P-03, P-04, P-09, and P-17.

- `api/screener/evidence.py::EvidenceLoader` owns the complete retained input bundle.
- `api/screener/sync.py::_derive_evidence` owns canonical derivation and serialization.
- `api/screener/artifacts.py::store_evidence` owns verified S3 writes and PostgreSQL artifact registration.
- `api/screener/postgres.py` and Alembic own the PostgreSQL schema.
- `api/screener/api.py` owns FastAPI serialization.

The import will call these owners. It will not copy financial rules or add a second parser.
Each company import uploads retained inputs, reads the verified artifacts back, derives through
the current engine, writes one immutable snapshot, and atomically selects it as current.
Retries reuse artifact hashes and snapshot hashes. Earlier snapshots remain stored.

The first proof set is NTES plus Panasonic `6752.T`. NTES covers SEC foreign/ADS identity;
Panasonic covers retained EDINET XBRL, JPY, and primary ordinary-share identity.
