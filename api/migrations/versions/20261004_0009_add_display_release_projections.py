"""Persist complete immutable display projections and input manifests."""
from alembic import op
import sqlalchemy as sa


revision = "20261004_0009"
down_revision = "20261004_0008"
branch_labels = None
depends_on = None


# Frozen declaration: later runtime schema edits cannot rewrite this migration.
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Double, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, JSON, PrimaryKeyConstraint, SmallInteger, Table, Text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from sqlalchemy import MetaData
metadata = MetaData()
Table("release_company", metadata, Column("release_id", BigInteger), Column("security_id", BigInteger))
Table("evidence_artifact", metadata, Column("content_sha256", Text))


QUERY_COLLATION = "market_screener_en_us"
# Existing persistence unit fixtures create shared metadata on SQLite.
_ordered_text = Text(collation=QUERY_COLLATION).with_variant(Text(), "sqlite")
release_company_projection = Table(
    "release_company_projection", metadata,
    Column("release_id", BigInteger, nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("projection_revision", Integer, nullable=False),
    Column("enriched_payload", JSONB, nullable=False),
    Column("assumed_payload", JSONB),
    Column("assumption_status", Text, nullable=False),
    Column("input_bundle_sha256", Text, ForeignKey("evidence_artifact.content_sha256", name="fk_company_projection_input_bundle"), nullable=False),
    Column("universe_ordinal", BigInteger, nullable=False),
    Column("ticker", _ordered_text, nullable=False),
    Column("name", _ordered_text, nullable=False),
    Column("sector", _ordered_text),
    Column("exchange", Text),
    Column("profile", Text, nullable=False),
    Column("search_hay", Text, nullable=False),
    Column("index_memberships", ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False),
    Column("enterprising_verdict", Text),
    Column("defensive_verdict", Text),
    Column("enterprising_gap", Text, nullable=False),
    Column("criteria_passed", SmallInteger, nullable=False),
    Column("positive_eps_years", SmallInteger),
    Column("unjudged", Boolean, nullable=False),
    Column("inapplicable", Boolean, nullable=False),
    Column("net_net", Boolean, nullable=False),
    *(Column(name, Double) for name in
      ("market_cap", "price", "off_high", "pe", "pe3", "dividend_yield")),
    Column("return_quality", Double, nullable=False),
    PrimaryKeyConstraint("release_id", "security_id", name="pk_release_company_projection"),
    ForeignKeyConstraint(["release_id", "security_id"], ["release_company.release_id", "release_company.security_id"], name="fk_company_projection_release_company"),
    CheckConstraint("projection_revision > 0", name="ck_company_projection_revision"),
    CheckConstraint("universe_ordinal >= 0", name="ck_company_projection_ordinal"),
    CheckConstraint("criteria_passed BETWEEN 0 AND 6", name="ck_company_projection_criteria"),
    CheckConstraint("positive_eps_years BETWEEN 0 AND 10", name="ck_company_projection_eps"),
    CheckConstraint("return_quality BETWEEN 0 AND 100", name="ck_company_projection_quality"),
    CheckConstraint("assumption_status IN ('APPLIED','UNAVAILABLE')", name="ck_company_projection_assumption"),
    CheckConstraint("assumption_status <> 'APPLIED' OR assumed_payload IS NOT NULL", name="ck_company_projection_assumed_payload"),
    CheckConstraint("jsonb_typeof(enriched_payload) = 'object'", name="ck_company_projection_payload").ddl_if(dialect="postgresql"),
)
Index("uq_company_projection_ticker", release_company_projection.c.release_id,
      release_company_projection.c.ticker.collate("C"), unique=True).ddl_if(dialect="postgresql")
Index("ix_company_projection_ticker", release_company_projection.c.release_id,
      release_company_projection.c.ticker, release_company_projection.c.security_id)
for name in ("pe", "pe3"):
    Index(f"ix_company_projection_positive_{name}", release_company_projection.c.release_id,
          release_company_projection.c[name], release_company_projection.c.ticker,
          release_company_projection.c.security_id,
          postgresql_where=release_company_projection.c[name] > 0)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("CREATE COLLATION market_screener_en_us (provider=icu, locale='en-US', deterministic=false)")
    op.add_column("data_release", sa.Column("projection_revision", sa.Integer()))
    op.add_column("data_release", sa.Column("input_bundle_sha256", sa.String(64)))
    op.create_foreign_key("fk_data_release_input_bundle", "data_release", "evidence_artifact", ["input_bundle_sha256"], ["content_sha256"])
    op.create_check_constraint("ck_data_release_projection", "data_release", "(projection_revision IS NULL AND input_bundle_sha256 IS NULL) OR (projection_revision IS NOT NULL AND projection_revision > 0 AND input_bundle_sha256 IS NOT NULL)")
    release_company_projection.create(op.get_bind())
    op.execute("CREATE TRIGGER freeze_release_company_projection BEFORE INSERT OR UPDATE OR DELETE ON release_company_projection FOR EACH ROW EXECUTE FUNCTION freeze_publication_members()")
    op.execute("""
        CREATE FUNCTION validate_display_release_seal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.published_at IS NOT NULL AND OLD.published_at IS NULL THEN
                IF NEW.projection_revision IS NULL THEN
                    IF EXISTS (SELECT 1 FROM release_company_projection WHERE release_id=NEW.release_id) THEN
                        RAISE EXCEPTION 'canonical release cannot contain display rows' USING ERRCODE='23514';
                    END IF;
                ELSE
                    IF NEW.projection_revision <> 1 OR
                       (SELECT count(*) FROM release_company WHERE release_id=NEW.release_id) = 0 OR
                       EXISTS (SELECT 1 FROM release_company c LEFT JOIN release_company_projection p
                               ON p.release_id=c.release_id AND p.security_id=c.security_id
                               WHERE c.release_id=NEW.release_id AND (p.security_id IS NULL OR p.projection_revision<>NEW.projection_revision)) THEN
                        RAISE EXCEPTION 'display release requires complete supported projection coverage' USING ERRCODE='23514';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER validate_display_release_seal BEFORE UPDATE ON data_release FOR EACH ROW EXECUTE FUNCTION validate_display_release_seal()")


def downgrade():
    raise RuntimeError("Display release history is retained; use a forward migration")
