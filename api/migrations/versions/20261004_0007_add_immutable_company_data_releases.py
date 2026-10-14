"""Add immutable company data releases

Revision ID: 20261004_0007
Revises: 20260929_0006
Create Date: 2026-10-04 09:37:39.088888
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261004_0007'
down_revision: Union[str, Sequence[str], None] = '20260929_0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table('data_release',
    sa.Column('release_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('parent_release_id', sa.BigInteger(), nullable=True),
    sa.Column('request_key', sa.Text(), nullable=False),
    sa.Column('publishing_job_id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(trim(request_key)) > 0', name='ck_data_release_request'),
    sa.CheckConstraint('parent_release_id IS NULL OR parent_release_id <> release_id', name='ck_data_release_parent'),
    sa.ForeignKeyConstraint(['parent_release_id'], ['data_release.release_id'], name='fk_data_release_parent'),
    sa.ForeignKeyConstraint(['publishing_job_id'], ['durable_job.job_id'], name='fk_data_release_job'),
    sa.PrimaryKeyConstraint('release_id', name='pk_data_release'),
    sa.UniqueConstraint('request_key', name='uq_data_release_request')
    )
    op.create_table('active_data_release',
    sa.Column('singleton_id', sa.Integer(), nullable=False),
    sa.Column('release_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint('singleton_id = 1', name='ck_active_data_release_singleton'),
    sa.ForeignKeyConstraint(['release_id'], ['data_release.release_id'], name='fk_active_data_release_release'),
    sa.PrimaryKeyConstraint('singleton_id', name='pk_active_data_release')
    )
    op.create_table('publication_candidate',
    sa.Column('candidate_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('request_key', sa.Text(), nullable=False),
    sa.Column('security_id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.BigInteger(), nullable=False),
    sa.Column('expected_parent_release_id', sa.BigInteger(), nullable=False),
    sa.Column('cause_type', sa.Text(), nullable=False),
    sa.Column('registering_job_id', sa.BigInteger(), nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("cause_type IN ('company_correction', 'source_change', 'extraction_fix', 'calculation_change')", name='ck_publication_candidate_cause'),
    sa.CheckConstraint('length(trim(request_key)) > 0', name='ck_publication_candidate_request'),
    sa.ForeignKeyConstraint(['expected_parent_release_id'], ['data_release.release_id'], name='fk_publication_candidate_parent'),
    sa.ForeignKeyConstraint(['registering_job_id'], ['durable_job.job_id'], name='fk_publication_candidate_job'),
    sa.ForeignKeyConstraint(['security_id', 'snapshot_id'], ['company_snapshot.security_id', 'company_snapshot.snapshot_id'], name='fk_publication_candidate_snapshot'),
    sa.PrimaryKeyConstraint('candidate_id', name='pk_publication_candidate'),
    sa.UniqueConstraint('request_key', name='uq_publication_candidate_request')
    )
    op.create_table('release_company',
    sa.Column('release_id', sa.BigInteger(), nullable=False),
    sa.Column('security_id', sa.BigInteger(), nullable=False),
    sa.Column('snapshot_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['release_id'], ['data_release.release_id'], name='fk_release_company_release'),
    sa.ForeignKeyConstraint(['security_id', 'snapshot_id'], ['company_snapshot.security_id', 'company_snapshot.snapshot_id'], name='fk_release_company_snapshot'),
    sa.PrimaryKeyConstraint('release_id', 'security_id', name='pk_release_company')
    )
    op.create_table('publication_candidate_source',
    sa.Column('candidate_id', sa.BigInteger(), nullable=False),
    sa.Column('source_observation_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['publication_candidate.candidate_id'], name='fk_publication_candidate_source_candidate'),
    sa.ForeignKeyConstraint(['source_observation_id'], ['source_observation.source_observation_id'], name='fk_publication_candidate_source_observation'),
    sa.PrimaryKeyConstraint('candidate_id', 'source_observation_id', name='pk_publication_candidate_source')
    )
    op.create_table('release_candidate',
    sa.Column('release_id', sa.BigInteger(), nullable=False),
    sa.Column('candidate_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['publication_candidate.candidate_id'], name='fk_release_candidate_candidate'),
    sa.ForeignKeyConstraint(['release_id'], ['data_release.release_id'], name='fk_release_candidate_release'),
    sa.PrimaryKeyConstraint('release_id', 'candidate_id', name='pk_release_candidate'),
    sa.UniqueConstraint('candidate_id', name='uq_release_candidate_once')
    )
    op.execute("INSERT INTO active_data_release (singleton_id) VALUES (1)")
    op.execute("""
        CREATE FUNCTION freeze_publication_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND (to_jsonb(NEW)->>TG_ARGV[0]) IS NULL THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND (to_jsonb(OLD)->>TG_ARGV[0]) IS NULL
               AND (to_jsonb(NEW)->>TG_ARGV[0]) IS NOT NULL
               AND (to_jsonb(OLD) - TG_ARGV[0]) = (to_jsonb(NEW) - TG_ARGV[0]) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'publication records are immutable' USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE FUNCTION freeze_publication_members() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE sealed_at timestamptz;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'publication membership is immutable' USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'publication_candidate_source' THEN
                SELECT registered_at INTO sealed_at FROM publication_candidate
                    WHERE candidate_id = NEW.candidate_id FOR SHARE;
            ELSE
                SELECT published_at INTO sealed_at FROM data_release
                    WHERE release_id = NEW.release_id FOR SHARE;
            END IF;
            IF sealed_at IS NOT NULL THEN
                RAISE EXCEPTION 'sealed publication membership is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION guard_active_data_release() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE parent_id bigint; activated_at timestamptz;
        BEGIN
            IF TG_OP = 'DELETE' OR NEW.release_id IS NULL THEN
                RAISE EXCEPTION 'active release cannot be removed' USING ERRCODE = '23514';
            END IF;
            SELECT parent_release_id, published_at INTO parent_id, activated_at
                FROM data_release WHERE release_id = NEW.release_id;
            IF activated_at IS NULL OR parent_id IS DISTINCT FROM OLD.release_id THEN
                RAISE EXCEPTION 'active release must be a published child of the current parent'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION guard_bootstrap_selection() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE selected_release bigint;
        BEGIN
            SELECT release_id INTO selected_release FROM active_data_release
                WHERE singleton_id = 1 FOR UPDATE;
            IF selected_release IS NOT NULL THEN
                RAISE EXCEPTION 'company selections require publication' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION lock_observed_source_item() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM source_item_id FROM source_item
                WHERE source_item_id = NEW.source_item_id FOR UPDATE;
            RETURN NEW;
        END $$
    """)
    for table, column in (("data_release", "published_at"), ("publication_candidate", "registered_at")):
        op.execute(f"CREATE TRIGGER freeze_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION freeze_publication_record('{column}')")
    for table in ("release_company", "release_candidate", "publication_candidate_source"):
        op.execute(f"CREATE TRIGGER freeze_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION freeze_publication_members()")
    op.execute("CREATE TRIGGER guard_active_data_release BEFORE UPDATE OR DELETE ON active_data_release "
               "FOR EACH ROW EXECUTE FUNCTION guard_active_data_release()")
    op.execute("CREATE TRIGGER guard_bootstrap_selection BEFORE INSERT OR UPDATE OR DELETE ON current_company_snapshot "
               "FOR EACH ROW EXECUTE FUNCTION guard_bootstrap_selection()")
    op.execute("CREATE TRIGGER lock_observed_source_item BEFORE INSERT ON source_observation "
               "FOR EACH ROW EXECUTE FUNCTION lock_observed_source_item()")


def downgrade() -> None:
    raise RuntimeError("Published financial revisions are retained history; use a forward migration")
