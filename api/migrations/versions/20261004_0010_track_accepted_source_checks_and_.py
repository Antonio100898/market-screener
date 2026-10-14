"""track accepted source checks and release lineage

Revision ID: 20261004_0010
Revises: 20261004_0009
Create Date: 2026-10-04 10:43:31.967711
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261004_0010'
down_revision: Union[str, Sequence[str], None] = '20261004_0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table('source_validity_generation',
    sa.Column('singleton_id', sa.Integer(), nullable=False),
    sa.Column('generation', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('singleton_id = 1 AND generation >= 0', name='ck_source_validity_generation'),
    sa.PrimaryKeyConstraint('singleton_id', name='pk_source_validity_generation')
    )
    op.create_table('source_check_occurrence',
    sa.Column('occurrence_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('source_item_id', sa.BigInteger(), nullable=False),
    sa.Column('source_observation_id', sa.BigInteger(), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('ownership_generation', sa.Integer(), nullable=False),
    sa.Column('item_key', sa.Text(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(trim(item_key)) > 0', name='ck_source_check_item_key'),
    sa.CheckConstraint('ownership_generation > 0', name='ck_source_check_generation'),
    sa.ForeignKeyConstraint(['job_id'], ['durable_job.job_id'], name='fk_source_check_job'),
    sa.ForeignKeyConstraint(['source_item_id'], ['source_item.source_item_id'], name='fk_source_check_item'),
    sa.ForeignKeyConstraint(['source_observation_id'], ['source_observation.source_observation_id'], name='fk_source_check_observation'),
    sa.PrimaryKeyConstraint('occurrence_id', name='pk_source_check_occurrence'),
    sa.UniqueConstraint('job_id', 'ownership_generation', 'item_key', 'source_observation_id', name='uq_source_check_delivery'),
    sa.UniqueConstraint('occurrence_id', 'source_item_id', 'source_observation_id', name='uq_source_check_pointer')
    )
    op.create_index('ix_source_check_item_time', 'source_check_occurrence', ['source_item_id', 'checked_at', 'occurrence_id'], unique=False)
    op.create_table('release_company_lineage',
    sa.Column('release_id', sa.BigInteger(), nullable=False),
    sa.Column('security_id', sa.BigInteger(), nullable=False),
    sa.Column('assessment', sa.Text(), nullable=False),
    sa.CheckConstraint("assessment IN ('COMPLETE', 'NOT_ASSESSED')", name='ck_release_company_lineage'),
    sa.ForeignKeyConstraint(['release_id', 'security_id'], ['release_company.release_id', 'release_company.security_id'], name='fk_release_lineage_member'),
    sa.PrimaryKeyConstraint('release_id', 'security_id', name='pk_release_company_lineage')
    )
    op.create_table('source_item_current',
    sa.Column('source_item_id', sa.BigInteger(), nullable=False),
    sa.Column('source_observation_id', sa.BigInteger(), nullable=False),
    sa.Column('occurrence_id', sa.BigInteger(), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('(occurrence_id IS NULL AND checked_at IS NULL) OR (occurrence_id IS NOT NULL AND checked_at IS NOT NULL)', name='ck_source_current_check'),
    sa.ForeignKeyConstraint(['occurrence_id', 'source_item_id', 'source_observation_id'], ['source_check_occurrence.occurrence_id', 'source_check_occurrence.source_item_id', 'source_check_occurrence.source_observation_id'], name='fk_source_current_occurrence'),
    sa.ForeignKeyConstraint(['source_item_id'], ['source_item.source_item_id'], name='fk_source_current_item'),
    sa.ForeignKeyConstraint(['source_observation_id'], ['source_observation.source_observation_id'], name='fk_source_current_observation'),
    sa.PrimaryKeyConstraint('source_item_id', name='pk_source_item_current')
    )
    op.create_table('release_company_source',
    sa.Column('release_id', sa.BigInteger(), nullable=False),
    sa.Column('security_id', sa.BigInteger(), nullable=False),
    sa.Column('source_observation_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['release_id', 'security_id'], ['release_company_lineage.release_id', 'release_company_lineage.security_id'], name='fk_release_source_lineage'),
    sa.ForeignKeyConstraint(['source_observation_id'], ['source_observation.source_observation_id'], name='fk_release_source_observation'),
    sa.PrimaryKeyConstraint('release_id', 'security_id', 'source_observation_id', name='pk_release_company_source')
    )
    op.create_index('ix_release_company_source_observation', 'release_company_source', ['source_observation_id', 'release_id', 'security_id'], unique=False)
    op.execute('INSERT INTO source_validity_generation (singleton_id, generation) VALUES (1, 0)')
    op.execute('''
        INSERT INTO source_item_current (source_item_id, source_observation_id)
        SELECT DISTINCT ON (source_item_id) source_item_id, source_observation_id
        FROM source_observation ORDER BY source_item_id, detected_at DESC, source_observation_id DESC
    ''')
    op.execute('''
        INSERT INTO release_company_lineage (release_id, security_id, assessment)
        SELECT release_id, security_id, 'NOT_ASSESSED' FROM release_company
    ''')
    op.execute('''
        CREATE FUNCTION guard_source_check_occurrence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'accepted source checks are immutable' USING ERRCODE = '23514';
            END IF;
            PERFORM source_item_id FROM source_item WHERE source_item_id = NEW.source_item_id FOR UPDATE;
            IF NOT EXISTS (SELECT 1 FROM source_observation
                WHERE source_observation_id = NEW.source_observation_id AND source_item_id = NEW.source_item_id) THEN
                RAISE EXCEPTION 'source check identifies another item' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    ''')
    op.execute('''
        CREATE FUNCTION guard_source_current_pointer() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE occurrence_time timestamptz;
        BEGIN
            IF TG_OP = 'DELETE' OR NEW.occurrence_id IS NULL THEN
                RAISE EXCEPTION 'source current requires an accepted check' USING ERRCODE = '23514';
            END IF;
            SELECT checked_at INTO occurrence_time FROM source_check_occurrence
                WHERE occurrence_id = NEW.occurrence_id AND source_item_id = NEW.source_item_id
                    AND source_observation_id = NEW.source_observation_id;
            IF occurrence_time IS NULL OR occurrence_time <> NEW.checked_at THEN
                RAISE EXCEPTION 'source pointer differs from accepted check' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.source_item_id <> OLD.source_item_id THEN
                RAISE EXCEPTION 'source pointer identity is immutable' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.checked_at IS NOT NULL AND
                (NEW.checked_at < OLD.checked_at OR
                 (NEW.checked_at = OLD.checked_at AND NEW.occurrence_id <= OLD.occurrence_id)) THEN
                RAISE EXCEPTION 'source pointer cannot move backward' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    ''')
    op.execute('CREATE TRIGGER guard_source_check_occurrence BEFORE INSERT OR UPDATE OR DELETE ON source_check_occurrence '
               'FOR EACH ROW EXECUTE FUNCTION guard_source_check_occurrence()')
    op.execute('CREATE TRIGGER guard_source_current_pointer BEFORE INSERT OR UPDATE OR DELETE ON source_item_current '
               'FOR EACH ROW EXECUTE FUNCTION guard_source_current_pointer()')
    for table in ('release_company_lineage', 'release_company_source'):
        op.execute(f'CREATE TRIGGER freeze_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION freeze_publication_members()')


def downgrade() -> None:
    raise RuntimeError('Accepted source checks and release lineage are retained; use a forward migration')
