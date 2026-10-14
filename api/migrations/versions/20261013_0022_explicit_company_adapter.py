"""Admit the existing explicit Adidas company adapter without remapping sources."""
from alembic import op

revision = "20261013_0022"
down_revision = "20261009_0021"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.drop_constraint("ck_issuer_source_system", "issuer", type_="check")
    op.create_check_constraint("ck_issuer_source_system", "issuer",
        "source_system IN ('SEC', 'EDINET', 'COMPANY_ADAPTER') "
        "AND (source_system <> 'COMPANY_ADAPTER' OR source_identifier = 'IFRS-ADIDAS-AG')")
    op.drop_constraint("ck_source_item_source_system", "source_item", type_="check")
    op.create_check_constraint("ck_source_item_source_system", "source_item",
        "source_system IN ('SEC', 'EDINET', 'COMPANY_ADAPTER') "
        "AND (source_system <> 'COMPANY_ADAPTER' OR "
        "(issuer_source_identifier IS NOT NULL AND issuer_source_identifier = 'IFRS-ADIDAS-AG'))")


def downgrade():
    raise RuntimeError("Explicit company adapter evidence is retained; use a forward migration")
