from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from screener.postgres import metadata
from screener import company_query_schema  # Registers display tables on the same metadata.
from screener.storage_config import StorageSettings


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    StorageSettings.from_env().database_url.replace("%", "%%"),
)
target_metadata = metadata


def include_object(obj, name, type_, reflected, compare_to):
    # SQLAlchemy reflection omits a plain-column index's override collation.
    # Compare this one definition to the catalog; every mismatch stays visible.
    if type_ == "index" and name == "uq_company_projection_ticker" and compare_to is not None:
        from sqlalchemy import text
        expected = obj if not reflected else compare_to
        catalog_matches = context.get_bind().scalar(text("""
            SELECT i.indisunique AND i.indnkeyatts=2 AND i.indnatts=2
                   AND i.indpred IS NULL AND i.indexprs IS NULL
                   AND pg_get_indexdef(i.indexrelid,1,true)='release_id'
                   AND pg_get_indexdef(i.indexrelid,2,true)='ticker'
                   AND c.collname='C'
            FROM pg_index i
            JOIN pg_collation c ON c.oid=i.indcollation[1]
            WHERE i.indexrelid=to_regclass(:index_name)
        """), {"index_name": name})
        if (catalog_matches and expected.unique and len(expected.expressions) == 2
                and str(expected.expressions[0]) == "release_company_projection.release_id"
                and str(expected.expressions[1]) == 'release_company_projection.ticker COLLATE "C"'):
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
