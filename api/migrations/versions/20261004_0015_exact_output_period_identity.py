"""Bind output uniqueness to actual periods; preserve all retained records."""
from alembic import op

revision = "20261004_0015"
down_revision = "20261004_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("LOCK TABLE figure_selection IN SHARE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM figure_selection
            WHERE COALESCE(record ->> 'purpose', 'OUTPUT') = 'OUTPUT'
            GROUP BY bundle_id, metric_path, view,
                record -> 'period' ->> 'kind', record -> 'period' ->> 'start', record -> 'period' ->> 'end'
            HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION 'Duplicate exact OUTPUT actual periods prevent0015; retained records require explicit reconciliation';
        END IF;
    END $$""")
    op.execute("DROP INDEX uq_figure_selection_output")
    op.execute("""CREATE UNIQUE INDEX uq_figure_selection_output ON figure_selection
        (bundle_id, metric_path, view, (record -> 'period' ->> 'kind'),
         (record -> 'period' ->> 'start'), (record -> 'period' ->> 'end'))
        NULLS NOT DISTINCT WHERE COALESCE(record ->> 'purpose', 'OUTPUT') = 'OUTPUT'""")


def downgrade():
    raise RuntimeError("Exact history is retained; use a forward migration")
