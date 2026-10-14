"""add private workspace revisions

Revision ID: 20261004_0008
Revises: 20261004_0007
Create Date: 2026-10-04 09:51:46.462191
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '20261004_0008'
down_revision: Union[str, Sequence[str], None] = '20261004_0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table('research_user',
    sa.Column('user_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'ACTIVE'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name='ck_research_user_status'),
    sa.PrimaryKeyConstraint('user_id', name='pk_research_user')
    )
    op.create_table('research_identity',
    sa.Column('issuer', sa.Text(), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('length(trim(issuer)) BETWEEN 1 AND 500', name='ck_research_identity_issuer'),
    sa.CheckConstraint('length(trim(subject)) BETWEEN 1 AND 500', name='ck_research_identity_subject'),
    sa.ForeignKeyConstraint(['user_id'], ['research_user.user_id'], name='fk_research_identity_user'),
    sa.PrimaryKeyConstraint('issuer', 'subject', name='pk_research_identity')
    )
    op.create_index('ix_research_identity_user', 'research_identity', ['user_id'], unique=False)
    op.create_table('research_workspace',
    sa.Column('workspace_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('owner_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.Text(), server_default=sa.text("'Default'"), nullable=False),
    sa.Column('schema_revision', sa.Integer(), nullable=False),
    sa.Column('edit_revision', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('edit_revision > 0', name='ck_research_workspace_revision'),
    sa.CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_research_workspace_name'),
    sa.CheckConstraint('schema_revision = 1', name='ck_research_workspace_schema'),
    sa.ForeignKeyConstraint(['owner_id'], ['research_user.user_id'], name='fk_research_workspace_owner'),
    sa.PrimaryKeyConstraint('workspace_id', name='pk_research_workspace'),
    sa.UniqueConstraint('owner_id', name='uq_research_workspace_owner'),
    sa.UniqueConstraint('workspace_id', 'owner_id', name='uq_research_workspace_identity_owner')
    )
    op.create_table('research_workspace_revision',
    sa.Column('workspace_id', sa.BigInteger(), nullable=False),
    sa.Column('owner_id', sa.BigInteger(), nullable=False),
    sa.Column('edit_revision', sa.BigInteger(), nullable=False),
    sa.Column('schema_revision', sa.Integer(), nullable=False),
    sa.Column('configuration', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('request_key', sa.Text(), nullable=False),
    sa.Column('request_sha256', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("actor IN ('USER', 'AGENT', 'SYSTEM')", name='ck_research_workspace_revision_actor'),
    sa.CheckConstraint("jsonb_typeof(configuration) = 'object' AND octet_length(configuration::text) <= 65536", name='ck_research_workspace_revision_configuration'),
    sa.CheckConstraint('edit_revision > 0', name='ck_research_workspace_revision_number'),
    sa.CheckConstraint('length(request_sha256) = 64', name='ck_research_workspace_revision_sha256'),
    sa.CheckConstraint('length(trim(request_key)) BETWEEN 1 AND 200', name='ck_research_workspace_revision_request'),
    sa.CheckConstraint('schema_revision = 1', name='ck_research_workspace_revision_schema'),
    sa.ForeignKeyConstraint(['workspace_id', 'owner_id'], ['research_workspace.workspace_id', 'research_workspace.owner_id'], name='fk_research_workspace_revision_owner'),
    sa.PrimaryKeyConstraint('workspace_id', 'edit_revision', name='pk_research_workspace_revision'),
    sa.UniqueConstraint('workspace_id', 'request_key', name='uq_research_workspace_revision_request')
    )
    op.create_foreign_key('fk_research_workspace_current_revision',
        'research_workspace', 'research_workspace_revision',
        ['workspace_id', 'edit_revision'], ['workspace_id', 'edit_revision'],
        deferrable=True, initially='DEFERRED')
    op.execute("""
        CREATE FUNCTION freeze_research_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'research history and identity are immutable' USING ERRCODE = '23514';
        END $$
    """)
    for table in ('research_identity', 'research_workspace_revision'):
        op.execute(f'CREATE TRIGGER freeze_{table} BEFORE UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION freeze_research_record()')
    op.execute("""
        CREATE FUNCTION guard_research_workspace_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.owner_id <> OLD.owner_id OR NEW.workspace_id <> OLD.workspace_id THEN
                RAISE EXCEPTION 'workspace ownership is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute('CREATE TRIGGER guard_research_workspace_owner BEFORE UPDATE ON research_workspace '
               'FOR EACH ROW EXECUTE FUNCTION guard_research_workspace_owner()')


def downgrade() -> None:
    raise RuntimeError('Private workspace history is retained; use a forward migration')
