# Local storage stack

Checked: 2026-09-25. Product decisions: P-02, P-03, P-04, P-09, P-14,
and P-17.

Use these exact local development versions:

- PostgreSQL `postgres:18.6-trixie@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`
- SeaweedFS `chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882`
- SQLAlchemy `2.0.54`
- Alembic `1.20.0`
- psycopg with binary package `3.3.6`
- boto3 `1.43.102`

PostgreSQL 18.6 is the current supported minor release. Use the Debian Trixie
image rather than PostgreSQL 19 beta. Persist `/var/lib/postgresql` and bind its
host port to `127.0.0.1`. Check readiness with `pg_isready`.

SeaweedFS is the local S3-compatible test service. Run one `weed mini` process,
persist `/data`, bind port 8333 to `127.0.0.1`, and supply local credentials and
the bucket through environment variables. Its quick start creates the named
bucket. MinIO is not selected because its open-source server repository is
archived and no longer maintained.

Use `postgresql+psycopg://` database URLs. SQLAlchemy owns connection pooling.
Use the stable 2.0 line rather than SQLAlchemy 2.1.1, which was released today.
Python and Alembic are the only application schema owners. Next.js will not gain
a PostgreSQL client.

Registry manifests and package indexes were checked without starting containers.
The Compose commands, health checks, persistence, migration, and real S3 behavior
remain unverified until the implementation increment runs.

Official sources:

- <https://www.postgresql.org/support/versioning/>
- <https://hub.docker.com/_/postgres>
- <https://www.postgresql.org/docs/current/app-pg-isready.html>
- <https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#psycopg>
- <https://alembic.sqlalchemy.org/en/latest/front.html>
- <https://www.psycopg.org/psycopg3/docs/basic/install.html>
- <https://github.com/seaweedfs/seaweedfs>
- <https://github.com/seaweedfs/seaweedfs/releases/tag/4.47>
- <https://github.com/minio/minio>
- <https://docs.docker.com/engine/storage/volumes/>
