"""add private research run lifecycle

Revision ID: 20261004_0011
Revises: 20261004_0010
Create Date: 2026-10-04 11:34:30.411854
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '20261004_0011'
down_revision: Union[str, Sequence[str], None] = '20261004_0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table('research_run',
    sa.Column('run_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('workspace_id', sa.BigInteger(), nullable=False),
    sa.Column('owner_id', sa.BigInteger(), nullable=False),
    sa.Column('initial_workspace_revision', sa.BigInteger(), nullable=False),
    sa.Column('release_id', sa.BigInteger(), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('request_key', sa.Text(), nullable=False),
    sa.Column('request_sha256', sa.String(length=64), nullable=False),
    sa.Column('request_text', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('last_event_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('lease_generation', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(state IN ('SUCCEEDED','FAILED','CANCELLED','DENIED')) = (finished_at IS NOT NULL)", name='ck_research_run_finished'),
    sa.CheckConstraint("state IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','DENIED')", name='ck_research_run_state'),
    sa.CheckConstraint("state NOT IN ('CANCEL_REQUESTED','CANCELLED') OR cancel_requested_at IS NOT NULL", name='ck_research_run_cancel'),
    sa.CheckConstraint('initial_workspace_revision > 0 AND last_event_seq >= 0 AND lease_generation >= 0', name='ck_research_run_revisions'),
    sa.CheckConstraint('length(trim(request_key)) BETWEEN 1 AND 200 AND length(request_sha256) = 64', name='ck_research_run_request'),
    sa.CheckConstraint('length(trim(request_text)) > 0 AND octet_length(request_text) <= 8000', name='ck_research_run_text'),
    sa.ForeignKeyConstraint(['job_id'], ['durable_job.job_id'], name='fk_research_run_job'),
    sa.ForeignKeyConstraint(['release_id'], ['data_release.release_id'], name='fk_research_run_release'),
    sa.ForeignKeyConstraint(['workspace_id', 'initial_workspace_revision'], ['research_workspace_revision.workspace_id', 'research_workspace_revision.edit_revision'], name='fk_research_run_revision'),
    sa.ForeignKeyConstraint(['workspace_id', 'owner_id'], ['research_workspace.workspace_id', 'research_workspace.owner_id'], name='fk_research_run_owner'),
    sa.PrimaryKeyConstraint('run_id', name='pk_research_run'),
    sa.UniqueConstraint('job_id', name='uq_research_run_job'),
    sa.UniqueConstraint('workspace_id', 'request_key', name='uq_research_run_request')
    )
    op.create_index('ix_research_run_owner_workspace', 'research_run', ['owner_id', 'workspace_id', 'run_id'], unique=False)
    op.create_index('ix_research_run_state', 'research_run', ['state', 'run_id'], unique=False)
    op.create_table('research_run_event',
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('event_key', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('lease_generation', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 8192", name='ck_research_run_event_payload'),
    sa.CheckConstraint("kind IN ('CREATED','STARTED','PROGRESS','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','DENIED')", name='ck_research_run_event_kind'),
    sa.CheckConstraint('length(trim(event_key)) BETWEEN 1 AND 240', name='ck_research_run_event_key'),
    sa.CheckConstraint('sequence > 0 AND lease_generation >= 0', name='ck_research_run_event_sequence'),
    sa.ForeignKeyConstraint(['run_id'], ['research_run.run_id'], name='fk_research_run_event_run'),
    sa.PrimaryKeyConstraint('run_id', 'sequence', name='pk_research_run_event'),
    sa.UniqueConstraint('run_id', 'event_key', name='uq_research_run_event_key')
    )
    op.execute('CREATE TRIGGER freeze_research_run_event BEFORE UPDATE OR DELETE ON research_run_event '
               'FOR EACH ROW EXECUTE FUNCTION freeze_research_record()')
    op.execute("""
        CREATE FUNCTION guard_research_run_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'research runs are retained' USING ERRCODE = '23514';
            END IF;
            IF ROW(NEW.run_id,NEW.workspace_id,NEW.owner_id,NEW.initial_workspace_revision,
                   NEW.release_id,NEW.job_id,NEW.request_key,NEW.request_sha256,NEW.request_text,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.run_id,OLD.workspace_id,OLD.owner_id,OLD.initial_workspace_revision,
                   OLD.release_id,OLD.job_id,OLD.request_key,OLD.request_sha256,OLD.request_text,OLD.created_at)
               OR OLD.state IN ('SUCCEEDED','FAILED','CANCELLED','DENIED')
               OR NEW.last_event_seq <> OLD.last_event_seq + 1
               OR NEW.lease_generation < OLD.lease_generation
               OR (OLD.state = 'CANCEL_REQUESTED' AND NEW.state NOT IN ('CANCELLED','DENIED'))
               OR (OLD.state = 'RUNNING' AND NEW.state = 'QUEUED') THEN
                RAISE EXCEPTION 'invalid research lifecycle transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute('CREATE TRIGGER guard_research_run_lifecycle BEFORE UPDATE OR DELETE ON research_run '
               'FOR EACH ROW EXECUTE FUNCTION guard_research_run_lifecycle()')


def downgrade() -> None:
    raise RuntimeError('Private research runs and events are retained; use a forward migration')
