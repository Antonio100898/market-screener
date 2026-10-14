"""Immutable release display fields; publication owns their insertion and sealing."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Double, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, JSON, PrimaryKeyConstraint, SmallInteger, Table, Text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from .postgres import metadata


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
