from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from .storage_config import StorageSettings


metadata = MetaData()

evidence_artifact = Table(
    "evidence_artifact",
    metadata,
    Column("content_sha256", String(64), primary_key=True),
    Column("object_key", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("media_type", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("byte_size >= 0", name="ck_evidence_artifact_byte_size"),
    UniqueConstraint("object_key", name="uq_evidence_artifact_object_key"),
)

issuer = Table(
    "issuer",
    metadata,
    Column("issuer_id", BigInteger, Identity(), nullable=False),
    Column("source_system", Text, nullable=False),
    Column("source_identifier", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "source_system IN ('SEC', 'EDINET', 'COMPANY_ADAPTER') "
        "AND (source_system <> 'COMPANY_ADAPTER' OR source_identifier = 'IFRS-ADIDAS-AG')",
        name="ck_issuer_source_system",
    ),
    CheckConstraint(
        "length(source_identifier) > 0",
        name="ck_issuer_source_identifier",
    ),
    PrimaryKeyConstraint("issuer_id", name="pk_issuer"),
    UniqueConstraint(
        "source_system",
        "source_identifier",
        name="uq_issuer_source_identifier",
    ),
)

priced_security = Table(
    "priced_security",
    metadata,
    Column("security_id", BigInteger, Identity(), nullable=False),
    Column(
        "issuer_id",
        BigInteger,
        ForeignKey("issuer.issuer_id", name="fk_priced_security_issuer"),
        nullable=False,
    ),
    Column("security_identifier", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "length(security_identifier) > 0",
        name="ck_priced_security_security_identifier",
    ),
    PrimaryKeyConstraint("security_id", name="pk_priced_security"),
    UniqueConstraint(
        "security_identifier",
        name="uq_priced_security_identifier",
    ),
)

company_snapshot = Table(
    "company_snapshot",
    metadata,
    Column("snapshot_id", BigInteger, Identity(), nullable=False),
    Column(
        "security_id",
        BigInteger,
        ForeignKey(
            "priced_security.security_id",
            name="fk_company_snapshot_security",
        ),
        nullable=False,
    ),
    Column("engine_revision", Integer, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("snapshot_sha256", String(64), nullable=False),
    Column("canonical_payload", JSONB, nullable=False),
    Column("ticker", Text, nullable=False),
    Column("exchange_code", Text),
    Column("quote_currency", String(3)),
    Column("security_title", Text),
    Column("source_accession", Text),
    Column("security_basis", Text, nullable=False),
    Column("receipt_ratio", Numeric, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "engine_revision > 0",
        name="ck_company_snapshot_engine_revision",
    ),
    CheckConstraint(
        "length(payload_sha256) = 64",
        name="ck_company_snapshot_payload_sha256",
    ),
    CheckConstraint(
        "length(snapshot_sha256) = 64",
        name="ck_company_snapshot_snapshot_sha256",
    ),
    CheckConstraint("length(ticker) > 0", name="ck_company_snapshot_ticker"),
    CheckConstraint(
        "exchange_code IS NULL OR length(exchange_code) > 0",
        name="ck_company_snapshot_exchange_code",
    ),
    CheckConstraint(
        "quote_currency IS NULL OR "
        "(length(quote_currency) = 3 AND quote_currency = upper(quote_currency))",
        name="ck_company_snapshot_quote_currency",
    ),
    CheckConstraint(
        "security_title IS NULL OR length(security_title) > 0",
        name="ck_company_snapshot_security_title",
    ),
    CheckConstraint(
        "source_accession IS NULL OR length(source_accession) > 0",
        name="ck_company_snapshot_source_accession",
    ),
    CheckConstraint(
        "length(security_basis) > 0",
        name="ck_company_snapshot_security_basis",
    ),
    CheckConstraint(
        "receipt_ratio IS NULL OR receipt_ratio > 0",
        name="ck_company_snapshot_receipt_ratio",
    ),
    PrimaryKeyConstraint("snapshot_id", name="pk_company_snapshot"),
    UniqueConstraint(
        "security_id",
        "snapshot_sha256",
        name="uq_company_snapshot_identity",
    ),
    UniqueConstraint(
        "security_id",
        "snapshot_id",
        name="uq_company_snapshot_security_snapshot",
    ),
)

company_snapshot_artifact = Table(
    "company_snapshot_artifact",
    metadata,
    Column("snapshot_id", BigInteger, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("role", Text, nullable=False),
    CheckConstraint(
        "length(role) > 0",
        name="ck_company_snapshot_artifact_role",
    ),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["company_snapshot.snapshot_id"],
        name="fk_company_snapshot_artifact_snapshot",
    ),
    ForeignKeyConstraint(
        ["content_sha256"],
        ["evidence_artifact.content_sha256"],
        name="fk_company_snapshot_artifact_evidence",
    ),
    PrimaryKeyConstraint(
        "snapshot_id",
        "content_sha256",
        "role",
        name="pk_company_snapshot_artifact",
    ),
)

current_company_snapshot = Table(
    "current_company_snapshot",
    metadata,
    Column("security_id", BigInteger, nullable=False),
    Column("snapshot_id", BigInteger, nullable=False),
    Column(
        "selected_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    ForeignKeyConstraint(
        ["security_id"],
        ["priced_security.security_id"],
        name="fk_current_company_snapshot_security",
    ),
    ForeignKeyConstraint(
        ["security_id", "snapshot_id"],
        ["company_snapshot.security_id", "company_snapshot.snapshot_id"],
        name="fk_current_company_snapshot_snapshot",
    ),
    PrimaryKeyConstraint("security_id", name="pk_current_company_snapshot"),
)

job_schedule_occurrence = Table(
    "job_schedule_occurrence",
    metadata,
    Column("occurrence_id", BigInteger, Identity(), nullable=False),
    Column("schedule_key", Text, nullable=False),
    Column("scheduled_for", DateTime(timezone=True), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "length(schedule_key) > 0",
        name="ck_job_schedule_occurrence_schedule_key",
    ),
    PrimaryKeyConstraint("occurrence_id", name="pk_job_schedule_occurrence"),
    UniqueConstraint(
        "schedule_key",
        "scheduled_for",
        name="uq_job_schedule_occurrence_identity",
    ),
)

durable_job = Table(
    "durable_job",
    metadata,
    Column("job_id", BigInteger, Identity(), nullable=False),
    Column(
        "occurrence_id",
        BigInteger,
        ForeignKey(
            "job_schedule_occurrence.occurrence_id",
            name="fk_durable_job_occurrence",
        ),
        nullable=True,
    ),
    Column("kind", Text, nullable=False),
    Column("parameters", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("next_retry_at", DateTime(timezone=True), nullable=True),
    Column("deadline_at", DateTime(timezone=True), nullable=True),
    Column("owner", Text, nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("ownership_generation", BigInteger, nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("checkpoint", JSONB, nullable=True),
    Column("error_summary", String(2000), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("length(kind) > 0", name="ck_durable_job_kind"),
    CheckConstraint(
        "status IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed')",
        name="ck_durable_job_status",
    ),
    CheckConstraint(
        "attempts >= 0 AND attempts <= max_attempts",
        name="ck_durable_job_attempts",
    ),
    CheckConstraint("max_attempts > 0", name="ck_durable_job_max_attempts"),
    CheckConstraint(
        "ownership_generation >= 0",
        name="ck_durable_job_ownership_generation",
    ),
    CheckConstraint(
        "deadline_at IS NULL OR deadline_at > due_at",
        name="ck_durable_job_deadline",
    ),
    CheckConstraint(
        "(status = 'running' AND owner IS NOT NULL "
        "AND length(owner) > 0 AND lease_expires_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND finished_at IS NULL) "
        "OR (status <> 'running' AND owner IS NULL AND lease_expires_at IS NULL)",
        name="ck_durable_job_active_ownership",
    ),
    CheckConstraint(
        "(status = 'retry_wait' AND next_retry_at IS NOT NULL) "
        "OR (status <> 'retry_wait' AND next_retry_at IS NULL)",
        name="ck_durable_job_retry_time",
    ),
    CheckConstraint(
        "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL) "
        "OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)",
        name="ck_durable_job_finished_time",
    ),
    CheckConstraint(
        "error_summary IS NULL OR length(error_summary) <= 2000",
        name="ck_durable_job_error_summary",
    ),
    PrimaryKeyConstraint("job_id", name="pk_durable_job"),
    UniqueConstraint("occurrence_id", name="uq_durable_job_occurrence"),
)

Index(
    "ix_durable_job_claim",
    durable_job.c.status,
    durable_job.c.due_at,
    durable_job.c.next_retry_at,
    durable_job.c.priority.desc(),
    durable_job.c.job_id,
)

durable_job_item = Table(
    "durable_job_item",
    metadata,
    Column(
        "job_id",
        BigInteger,
        ForeignKey("durable_job.job_id", name="fk_durable_job_item_job"),
        nullable=False,
    ),
    Column("item_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("outcome", JSONB, nullable=True),
    Column("error_summary", String(2000), nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(item_key) > 0", name="ck_durable_job_item_key"),
    CheckConstraint(
        "status IN ('succeeded', 'failed')",
        name="ck_durable_job_item_status",
    ),
    CheckConstraint("attempts > 0", name="ck_durable_job_item_attempts"),
    CheckConstraint(
        "error_summary IS NULL OR length(error_summary) <= 2000",
        name="ck_durable_job_item_error_summary",
    ),
    PrimaryKeyConstraint("job_id", "item_key", name="pk_durable_job_item"),
)

source_item = Table(
    "source_item",
    metadata,
    Column("source_item_id", BigInteger, Identity(), nullable=False),
    Column("source_system", Text, nullable=False),
    Column("item_kind", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("issuer_source_identifier", Text, nullable=True),
    Column(
        "parent_source_item_id",
        BigInteger,
        nullable=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "source_system IN ('SEC', 'EDINET', 'COMPANY_ADAPTER') "
        "AND (source_system <> 'COMPANY_ADAPTER' OR "
        "(issuer_source_identifier IS NOT NULL AND issuer_source_identifier = 'IFRS-ADIDAS-AG'))",
        name="ck_source_item_source_system",
    ),
    CheckConstraint(
        "item_kind IN ('inventory', 'aggregate', 'filing', 'resource')",
        name="ck_source_item_kind",
    ),
    CheckConstraint(
        "length(trim(source_key)) > 0",
        name="ck_source_item_source_key",
    ),
    CheckConstraint(
        "issuer_source_identifier IS NULL "
        "OR length(trim(issuer_source_identifier)) > 0",
        name="ck_source_item_issuer_source_identifier",
    ),
    CheckConstraint(
        "parent_source_item_id IS NULL OR parent_source_item_id <> source_item_id",
        name="ck_source_item_parent_not_self",
    ),
    PrimaryKeyConstraint("source_item_id", name="pk_source_item"),
    UniqueConstraint(
        "source_item_id",
        "source_system",
        name="uq_source_item_id_system",
    ),
    UniqueConstraint(
        "source_system",
        "item_kind",
        "source_key",
        name="uq_source_item_identity",
    ),
    ForeignKeyConstraint(
        ["parent_source_item_id", "source_system"],
        ["source_item.source_item_id", "source_item.source_system"],
        name="fk_source_item_parent",
    ),
)

source_observation = Table(
    "source_observation",
    metadata,
    Column("source_observation_id", BigInteger, Identity(), nullable=False),
    Column(
        "source_item_id",
        BigInteger,
        ForeignKey("source_item.source_item_id", name="fk_source_observation_item"),
        nullable=False,
    ),
    Column("observation_sha256", String(64), nullable=False),
    Column("state", Text, nullable=False),
    Column("canonical_metadata", JSONB, nullable=False),
    Column(
        "artifact_sha256",
        String(64),
        ForeignKey(
            "evidence_artifact.content_sha256",
            name="fk_source_observation_artifact",
        ),
        nullable=True,
    ),
    Column("source_url", Text, nullable=False),
    Column("etag", Text, nullable=True),
    Column("last_modified", Text, nullable=True),
    Column("source_published_at", DateTime(timezone=True), nullable=True),
    Column("detected_at", DateTime(timezone=True), nullable=False),
    Column(
        "detecting_job_id",
        BigInteger,
        ForeignKey("durable_job.job_id", name="fk_source_observation_job"),
        nullable=False,
    ),
    CheckConstraint(
        "observation_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_source_observation_sha256",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "state IN ('present', 'pending', 'unavailable', 'removed')",
        name="ck_source_observation_state",
    ),
    CheckConstraint(
        "jsonb_typeof(canonical_metadata) = 'object'",
        name="ck_source_observation_metadata_object",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "(state = 'present' AND artifact_sha256 IS NOT NULL) "
        "OR (state <> 'present' AND artifact_sha256 IS NULL)",
        name="ck_source_observation_state_artifact",
    ),
    CheckConstraint(
        "length(trim(source_url)) > 0",
        name="ck_source_observation_source_url",
    ),
    CheckConstraint(
        "etag IS NULL OR length(trim(etag)) > 0",
        name="ck_source_observation_etag",
    ),
    CheckConstraint(
        "last_modified IS NULL OR length(trim(last_modified)) > 0",
        name="ck_source_observation_last_modified",
    ),
    PrimaryKeyConstraint("source_observation_id", name="pk_source_observation"),
    UniqueConstraint(
        "source_item_id",
        "observation_sha256",
        name="uq_source_observation_identity",
    ),
)


data_release = Table(
    "data_release", metadata,
    Column("release_id", BigInteger, Identity(), nullable=False),
    Column("parent_release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_data_release_parent")),
    Column("request_key", Text, nullable=False),
    Column("publishing_job_id", BigInteger, ForeignKey("durable_job.job_id", name="fk_data_release_job"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False),
    Column("published_at", DateTime(timezone=True)),
    Column("projection_revision", Integer),
    Column("input_bundle_sha256", String(64), ForeignKey("evidence_artifact.content_sha256", name="fk_data_release_input_bundle")),
    Column("figure_history_revision", Integer),
    PrimaryKeyConstraint("release_id", name="pk_data_release"),
    UniqueConstraint("request_key", name="uq_data_release_request"),
    UniqueConstraint("release_id", "parent_release_id", name="uq_data_release_parent_binding"),
    CheckConstraint("figure_history_revision IS NULL OR figure_history_revision = 1", name="ck_data_release_figure_history_revision"),
    CheckConstraint("length(trim(request_key)) > 0", name="ck_data_release_request"),
    CheckConstraint("parent_release_id IS NULL OR parent_release_id <> release_id", name="ck_data_release_parent"),
    CheckConstraint("(projection_revision IS NULL AND input_bundle_sha256 IS NULL) OR (projection_revision IS NOT NULL AND projection_revision > 0 AND input_bundle_sha256 IS NOT NULL)", name="ck_data_release_projection"),
)

release_company = Table(
    "release_company", metadata,
    Column("release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_release_company_release"), nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("snapshot_id", BigInteger, nullable=False),
    Column("exact_bundle_id", BigInteger),
    PrimaryKeyConstraint("release_id", "security_id", name="pk_release_company"),
    UniqueConstraint("release_id", "security_id", "exact_bundle_id", name="uq_release_company_exact_binding"),
    ForeignKeyConstraint(["security_id", "snapshot_id"], ["company_snapshot.security_id", "company_snapshot.snapshot_id"], name="fk_release_company_snapshot"),
    ForeignKeyConstraint(["exact_bundle_id", "security_id", "snapshot_id"], ["exact_figure_bundle.bundle_id", "exact_figure_bundle.security_id", "exact_figure_bundle.snapshot_id"], name="fk_release_company_exact_bundle"),
)

active_data_release = Table(
    "active_data_release", metadata,
    Column("singleton_id", Integer, nullable=False),
    Column("release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_active_data_release_release")),
    PrimaryKeyConstraint("singleton_id", name="pk_active_data_release"),
    CheckConstraint("singleton_id = 1", name="ck_active_data_release_singleton"),
)

publication_candidate = Table(
    "publication_candidate", metadata,
    Column("candidate_id", BigInteger, Identity(), nullable=False),
    Column("request_key", Text, nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("snapshot_id", BigInteger, nullable=False),
    Column("exact_bundle_id", BigInteger),
    Column("expected_parent_release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_publication_candidate_parent")),
    Column("cause_type", Text, nullable=False),
    Column("registering_job_id", BigInteger, ForeignKey("durable_job.job_id", name="fk_publication_candidate_job"), nullable=False),
    Column("registered_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("candidate_id", name="pk_publication_candidate"),
    UniqueConstraint("request_key", name="uq_publication_candidate_request"),
    CheckConstraint("length(trim(request_key)) > 0", name="ck_publication_candidate_request"),
    CheckConstraint("cause_type IN ('company_correction', 'source_change', 'extraction_fix', 'calculation_change')", name="ck_publication_candidate_cause"),
    ForeignKeyConstraint(["security_id", "snapshot_id"], ["company_snapshot.security_id", "company_snapshot.snapshot_id"], name="fk_publication_candidate_snapshot"),
    ForeignKeyConstraint(["exact_bundle_id", "security_id", "snapshot_id"], ["exact_figure_bundle.bundle_id", "exact_figure_bundle.security_id", "exact_figure_bundle.snapshot_id"], name="fk_publication_candidate_exact_bundle"),
)

publication_candidate_source = Table(
    "publication_candidate_source", metadata,
    Column("candidate_id", BigInteger, ForeignKey("publication_candidate.candidate_id", name="fk_publication_candidate_source_candidate"), nullable=False),
    Column("source_observation_id", BigInteger, ForeignKey("source_observation.source_observation_id", name="fk_publication_candidate_source_observation"), nullable=False),
    PrimaryKeyConstraint("candidate_id", "source_observation_id", name="pk_publication_candidate_source"),
)

release_candidate = Table(
    "release_candidate", metadata,
    Column("release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_release_candidate_release"), nullable=False),
    Column("candidate_id", BigInteger, ForeignKey("publication_candidate.candidate_id", name="fk_release_candidate_candidate"), nullable=False),
    PrimaryKeyConstraint("release_id", "candidate_id", name="pk_release_candidate"),
    UniqueConstraint("candidate_id", name="uq_release_candidate_once"),
)


research_user = Table(
    "research_user", metadata,
    Column("user_id", BigInteger, Identity(), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("user_id", name="pk_research_user"),
    CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_research_user_status"),
)

research_identity = Table(
    "research_identity", metadata,
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("user_id", BigInteger, ForeignKey("research_user.user_id", name="fk_research_identity_user"), nullable=False),
    PrimaryKeyConstraint("issuer", "subject", name="pk_research_identity"),
    CheckConstraint("length(trim(issuer)) BETWEEN 1 AND 500", name="ck_research_identity_issuer"),
    CheckConstraint("length(trim(subject)) BETWEEN 1 AND 500", name="ck_research_identity_subject"),
    Index("ix_research_identity_user", "user_id"),
)

research_workspace = Table(
    "research_workspace", metadata,
    Column("workspace_id", BigInteger, Identity(), nullable=False),
    Column("owner_id", BigInteger, ForeignKey("research_user.user_id", name="fk_research_workspace_owner"), nullable=False),
    Column("name", Text, nullable=False, server_default=text("'Default'")),
    Column("schema_revision", Integer, nullable=False),
    Column("edit_revision", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("workspace_id", name="pk_research_workspace"),
    UniqueConstraint("owner_id", name="uq_research_workspace_owner"),
    UniqueConstraint("workspace_id", "owner_id", name="uq_research_workspace_identity_owner"),
    CheckConstraint("schema_revision = 1", name="ck_research_workspace_schema"),
    CheckConstraint("edit_revision > 0", name="ck_research_workspace_revision"),
    CheckConstraint("length(trim(name)) BETWEEN 1 AND 200", name="ck_research_workspace_name"),
    ForeignKeyConstraint(["workspace_id", "edit_revision"],
                         ["research_workspace_revision.workspace_id", "research_workspace_revision.edit_revision"],
                         name="fk_research_workspace_current_revision", use_alter=True,
                         deferrable=True, initially="DEFERRED"),
)

research_workspace_revision = Table(
    "research_workspace_revision", metadata,
    Column("workspace_id", BigInteger, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("edit_revision", BigInteger, nullable=False),
    Column("schema_revision", Integer, nullable=False),
    Column("configuration", JSONB, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("actor", Text, nullable=False),
    Column("restored_from_revision", BigInteger, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("workspace_id", "edit_revision", name="pk_research_workspace_revision"),
    ForeignKeyConstraint(["workspace_id", "owner_id"], ["research_workspace.workspace_id", "research_workspace.owner_id"],
                         name="fk_research_workspace_revision_owner"),
    UniqueConstraint("workspace_id", "request_key", name="uq_research_workspace_revision_request"),
    ForeignKeyConstraint(["workspace_id", "restored_from_revision"],
                         ["research_workspace_revision.workspace_id", "research_workspace_revision.edit_revision"],
                         name="fk_research_workspace_revision_restore"),
    CheckConstraint("restored_from_revision IS NULL OR (restored_from_revision > 0 AND restored_from_revision < edit_revision)",
                    name="ck_research_workspace_revision_restore"),
    CheckConstraint("edit_revision > 0", name="ck_research_workspace_revision_number"),
    CheckConstraint("schema_revision = 1", name="ck_research_workspace_revision_schema"),
    CheckConstraint("jsonb_typeof(configuration) = 'object' AND octet_length(configuration::text) <= 65536", name="ck_research_workspace_revision_configuration").ddl_if(dialect="postgresql"),
    CheckConstraint("length(trim(request_key)) BETWEEN 1 AND 200", name="ck_research_workspace_revision_request"),
    CheckConstraint("length(request_sha256) = 64", name="ck_research_workspace_revision_sha256"),
    CheckConstraint("actor IN ('USER', 'AGENT', 'SYSTEM')", name="ck_research_workspace_revision_actor"),
)


research_run = Table(
    "research_run", metadata,
    Column("run_id", BigInteger, Identity(), nullable=False),
    Column("workspace_id", BigInteger, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("initial_workspace_revision", BigInteger, nullable=False),
    Column("release_id", BigInteger, ForeignKey("data_release.release_id", name="fk_research_run_release"), nullable=False),
    Column("job_id", BigInteger, ForeignKey("durable_job.job_id", name="fk_research_run_job"), nullable=False),
    Column("request_key", Text, nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("request_text", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("last_event_seq", BigInteger, nullable=False, server_default=text("0")),
    Column("lease_generation", BigInteger, nullable=False, server_default=text("0")),
    Column("cancel_requested_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("finished_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("run_id", name="pk_research_run"),
    ForeignKeyConstraint(["workspace_id", "owner_id"], ["research_workspace.workspace_id", "research_workspace.owner_id"], name="fk_research_run_owner"),
    ForeignKeyConstraint(["workspace_id", "initial_workspace_revision"], ["research_workspace_revision.workspace_id", "research_workspace_revision.edit_revision"], name="fk_research_run_revision"),
    UniqueConstraint("workspace_id", "request_key", name="uq_research_run_request"),
    UniqueConstraint("job_id", name="uq_research_run_job"),
    CheckConstraint("state IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','DENIED')", name="ck_research_run_state"),
    CheckConstraint("(state IN ('SUCCEEDED','FAILED','CANCELLED','DENIED')) = (finished_at IS NOT NULL)", name="ck_research_run_finished"),
    CheckConstraint("state NOT IN ('CANCEL_REQUESTED','CANCELLED') OR cancel_requested_at IS NOT NULL", name="ck_research_run_cancel"),
    CheckConstraint("initial_workspace_revision > 0 AND last_event_seq >= 0 AND lease_generation >= 0", name="ck_research_run_revisions"),
    CheckConstraint("length(trim(request_key)) BETWEEN 1 AND 200 AND length(request_sha256) = 64", name="ck_research_run_request"),
    CheckConstraint("length(trim(request_text)) > 0 AND octet_length(request_text) <= 8000", name="ck_research_run_text").ddl_if(dialect="postgresql"),
    Index("ix_research_run_owner_workspace", "owner_id", "workspace_id", "run_id"),
    Index("ix_research_run_state", "state", "run_id"),
)

research_run_event = Table(
    "research_run_event", metadata,
    Column("run_id", BigInteger, ForeignKey("research_run.run_id", name="fk_research_run_event_run"), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("event_key", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("lease_generation", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("run_id", "sequence", name="pk_research_run_event"),
    UniqueConstraint("run_id", "event_key", name="uq_research_run_event_key"),
    CheckConstraint("sequence > 0 AND lease_generation >= 0", name="ck_research_run_event_sequence"),
    CheckConstraint("length(trim(event_key)) BETWEEN 1 AND 240", name="ck_research_run_event_key"),
    CheckConstraint("kind IN ('CREATED','STARTED','PROGRESS','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','DENIED')", name="ck_research_run_event_kind"),
    CheckConstraint("jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 8192", name="ck_research_run_event_payload").ddl_if(dialect="postgresql"),
)

source_check_occurrence = Table(
    "source_check_occurrence", metadata,
    Column("occurrence_id", BigInteger, Identity(), nullable=False),
    Column("source_item_id", BigInteger, ForeignKey("source_item.source_item_id", name="fk_source_check_item"), nullable=False),
    Column("source_observation_id", BigInteger, ForeignKey("source_observation.source_observation_id", name="fk_source_check_observation"), nullable=False),
    Column("job_id", BigInteger, ForeignKey("durable_job.job_id", name="fk_source_check_job"), nullable=False),
    Column("ownership_generation", Integer, nullable=False),
    Column("item_key", Text, nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("occurrence_id", name="pk_source_check_occurrence"),
    UniqueConstraint("job_id", "ownership_generation", "item_key", "source_observation_id", name="uq_source_check_delivery"),
    UniqueConstraint("occurrence_id", "source_item_id", "source_observation_id", name="uq_source_check_pointer"),
    CheckConstraint("ownership_generation > 0", name="ck_source_check_generation"),
    CheckConstraint("length(trim(item_key)) > 0", name="ck_source_check_item_key"),
    Index("ix_source_check_item_time", "source_item_id", "checked_at", "occurrence_id"),
    Index("ix_source_check_item_occurrence", "source_item_id", text("occurrence_id DESC")),
)

source_item_current = Table(
    "source_item_current", metadata,
    Column("source_item_id", BigInteger, ForeignKey("source_item.source_item_id", name="fk_source_current_item"), nullable=False),
    Column("source_observation_id", BigInteger, ForeignKey("source_observation.source_observation_id", name="fk_source_current_observation"), nullable=False),
    Column("occurrence_id", BigInteger),
    Column("checked_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("source_item_id", name="pk_source_item_current"),
    ForeignKeyConstraint(["occurrence_id", "source_item_id", "source_observation_id"],
                         ["source_check_occurrence.occurrence_id", "source_check_occurrence.source_item_id", "source_check_occurrence.source_observation_id"],
                         name="fk_source_current_occurrence"),
    CheckConstraint("(occurrence_id IS NULL AND checked_at IS NULL) OR (occurrence_id IS NOT NULL AND checked_at IS NOT NULL)", name="ck_source_current_check"),
)

source_validity_generation = Table(
    "source_validity_generation", metadata,
    Column("singleton_id", Integer, nullable=False),
    Column("generation", BigInteger, nullable=False),
    PrimaryKeyConstraint("singleton_id", name="pk_source_validity_generation"),
    CheckConstraint("singleton_id = 1 AND generation >= 0", name="ck_source_validity_generation"),
)

release_company_lineage = Table(
    "release_company_lineage", metadata,
    Column("release_id", BigInteger, nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("assessment", Text, nullable=False),
    PrimaryKeyConstraint("release_id", "security_id", name="pk_release_company_lineage"),
    ForeignKeyConstraint(["release_id", "security_id"], ["release_company.release_id", "release_company.security_id"], name="fk_release_lineage_member"),
    CheckConstraint("assessment IN ('COMPLETE', 'NOT_ASSESSED')", name="ck_release_company_lineage"),
)

release_company_source = Table(
    "release_company_source", metadata,
    Column("release_id", BigInteger, nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("source_observation_id", BigInteger, ForeignKey("source_observation.source_observation_id", name="fk_release_source_observation"), nullable=False),
    Column("activation_source_item_id", BigInteger),
    Column("accepted_occurrence_id", BigInteger),
    PrimaryKeyConstraint("release_id", "security_id", "source_observation_id", name="pk_release_company_source"),
    ForeignKeyConstraint(["release_id", "security_id"], ["release_company_lineage.release_id", "release_company_lineage.security_id"], name="fk_release_source_lineage"),
    CheckConstraint("(activation_source_item_id IS NULL) = (accepted_occurrence_id IS NULL)", name="ck_release_source_witness_pair"),
    ForeignKeyConstraint(["source_observation_id", "activation_source_item_id"], ["source_observation.source_observation_id", "source_observation.source_item_id"], name="fk_release_source_item_binding"),
    ForeignKeyConstraint(["accepted_occurrence_id", "activation_source_item_id", "source_observation_id"], ["source_check_occurrence.occurrence_id", "source_check_occurrence.source_item_id", "source_check_occurrence.source_observation_id"], name="fk_release_source_accepted_check"),
    Index("ix_release_company_source_observation", "source_observation_id", "release_id", "security_id"),
)


figure_observation = Table(
    "figure_observation", metadata,
    Column("observation_id", BigInteger, Identity(), nullable=False),
    Column("artifact_sha256", String(64), ForeignKey("evidence_artifact.content_sha256", name="fk_figure_observation_artifact"), nullable=False),
    Column("issuer_id", BigInteger, ForeignKey("issuer.issuer_id", name="fk_figure_observation_issuer"), nullable=False),
    Column("extraction_revision", Text, nullable=False),
    Column("location_sha256", String(64), nullable=False),
    Column("value_decimal_text", Text),
    Column("record", JSONB, nullable=False),
    PrimaryKeyConstraint("observation_id", name="pk_figure_observation"),
    UniqueConstraint("artifact_sha256", "extraction_revision", "location_sha256", name="uq_figure_observation_occurrence"),
    CheckConstraint("length(extraction_revision) > 0 AND length(location_sha256) = 64", name="ck_figure_observation_identity"),
)

exact_figure_bundle = Table(
    "exact_figure_bundle", metadata,
    Column("bundle_id", BigInteger, Identity(), nullable=False),
    Column("request_key", Text, nullable=False),
    Column("bundle_sha256", String(64), nullable=False),
    Column("security_id", BigInteger, nullable=False),
    Column("snapshot_id", BigInteger, nullable=False),
    Column("snapshot_sha256", String(64), nullable=False),
    Column("engine_revision", Integer, nullable=False),
    Column("pipeline_run_key", Text, nullable=False),
    Column("contract_revision", Integer, nullable=False),
    Column("observation_bindings", JSONB, nullable=False),
    Column("contract_details", JSONB, nullable=False, server_default=text("'{}'")),
    Column("storing_job_id", BigInteger, ForeignKey("durable_job.job_id", name="fk_exact_bundle_job"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False),
    Column("sealed_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("bundle_id", name="pk_exact_figure_bundle"),
    UniqueConstraint("request_key", name="uq_exact_figure_bundle_request"),
    ForeignKeyConstraint(["security_id", "snapshot_id"], ["company_snapshot.security_id", "company_snapshot.snapshot_id"], name="fk_exact_bundle_snapshot"),
    UniqueConstraint("bundle_id", "security_id", "snapshot_id", name="uq_exact_bundle_binding"),
    CheckConstraint("length(trim(request_key)) > 0 AND length(bundle_sha256) = 64 AND length(snapshot_sha256) = 64 AND engine_revision > 0 AND contract_revision IN (1,2,3) AND length(pipeline_run_key) > 0", name="ck_exact_bundle_identity"),
    CheckConstraint("jsonb_typeof(contract_details) = 'object' AND (contract_revision <> 1 OR contract_details = '{}'::jsonb)", name="ck_exact_bundle_details").ddl_if(dialect="postgresql"),
    Index("ix_exact_bundle_snapshot", "security_id", "snapshot_id"),
)

figure_selection = Table(
    "figure_selection", metadata,
    Column("bundle_id", BigInteger, ForeignKey("exact_figure_bundle.bundle_id", name="fk_figure_selection_bundle"), nullable=False),
    Column("node_key", Text, nullable=False),
    Column("metric_family", Text, nullable=False),
    Column("metric_path", Text, nullable=False),
    Column("view", Text, nullable=False),
    Column("value_decimal_text", Text),
    Column("observation_id", BigInteger, ForeignKey("figure_observation.observation_id", name="fk_figure_selection_observation")),
    Column("record", JSONB, nullable=False),
    PrimaryKeyConstraint("bundle_id", "node_key", name="pk_figure_selection"),
    CheckConstraint("view IN ('STRICT','ASSUMED','CONSERVATIVE')", name="ck_figure_selection_view"),
    Index("ix_figure_selection_metric", "bundle_id", "metric_path", "view"),
    Index("uq_figure_selection_output", "bundle_id", "metric_path", "view",
          text("((record -> 'period'::text) ->> 'kind'::text)"),
          text("((record -> 'period'::text) ->> 'start'::text)"),
          text("((record -> 'period'::text) ->> 'end'::text)"),
          unique=True, postgresql_nulls_not_distinct=True,
          postgresql_where=text("COALESCE(record ->> 'purpose', 'OUTPUT') = 'OUTPUT'")).ddl_if(dialect="postgresql"),
)

figure_dependency = Table(
    "figure_dependency", metadata,
    Column("bundle_id", BigInteger, nullable=False),
    Column("parent_node_key", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("observation_id", BigInteger, ForeignKey("figure_observation.observation_id", name="fk_figure_dependency_observation")),
    Column("input_node_key", Text),
    Column("record", JSONB, nullable=False),
    PrimaryKeyConstraint("bundle_id", "parent_node_key", "ordinal", name="pk_figure_dependency"),
    ForeignKeyConstraint(["bundle_id", "parent_node_key"], ["figure_selection.bundle_id", "figure_selection.node_key"], name="fk_figure_dependency_parent"),
    ForeignKeyConstraint(["bundle_id", "input_node_key"], ["figure_selection.bundle_id", "figure_selection.node_key"], name="fk_figure_dependency_input"),
    CheckConstraint("ordinal >= 0 AND NOT (observation_id IS NOT NULL AND input_node_key IS NOT NULL)", name="ck_figure_dependency_input"),
)

figure_bundle_coverage = Table(
    "figure_bundle_coverage", metadata,
    Column("bundle_id", BigInteger, ForeignKey("exact_figure_bundle.bundle_id", name="fk_figure_coverage_bundle"), nullable=False),
    Column("metric_family", Text, nullable=False),
    Column("view", Text, nullable=False),
    Column("record", JSONB, nullable=False),
    PrimaryKeyConstraint("bundle_id", "metric_family", "view", name="pk_figure_bundle_coverage"),
    CheckConstraint("view IN ('STRICT','ASSUMED','CONSERVATIVE')", name="ck_figure_coverage_view"),
)

source_observation.append_constraint(UniqueConstraint("source_observation_id", "source_item_id", name="uq_source_observation_item_binding"))
figure_bundle_source = Table(
    "figure_bundle_source", metadata,
    Column("bundle_id", BigInteger, ForeignKey("exact_figure_bundle.bundle_id", name="fk_figure_source_bundle"), nullable=False),
    Column("source_item_id", BigInteger, nullable=False),
    Column("source_observation_id", BigInteger, nullable=False),
    Column("occurrence_id", BigInteger),
    PrimaryKeyConstraint("bundle_id", "source_item_id", "source_observation_id", name="pk_figure_bundle_source"),
    ForeignKeyConstraint(["source_observation_id", "source_item_id"], ["source_observation.source_observation_id", "source_observation.source_item_id"], name="fk_figure_source_revision"),
    ForeignKeyConstraint(["occurrence_id", "source_item_id", "source_observation_id"], ["source_check_occurrence.occurrence_id", "source_check_occurrence.source_item_id", "source_check_occurrence.source_observation_id"], name="fk_figure_source_occurrence"),
    Index("ix_figure_source_revision", "source_observation_id", "bundle_id"),
)


document_evidence = Table(
    "document_evidence", metadata,
    Column("evidence_id", BigInteger, Identity(), nullable=False),
    Column("artifact_sha256", String(64), ForeignKey("evidence_artifact.content_sha256", name="fk_document_evidence_artifact"), nullable=False),
    Column("issuer_id", BigInteger, ForeignKey("issuer.issuer_id", name="fk_document_evidence_issuer"), nullable=False),
    Column("extraction_revision", Text, nullable=False),
    Column("location_sha256", String(64), nullable=False),
    Column("start_byte", BigInteger, nullable=False),
    Column("end_byte", BigInteger, nullable=False),
    Column("span_sha256", String(64), nullable=False),
    Column("record", JSONB, nullable=False),
    PrimaryKeyConstraint("evidence_id", name="pk_document_evidence"),
    UniqueConstraint("artifact_sha256", "extraction_revision", "location_sha256", name="uq_document_evidence_occurrence"),
    CheckConstraint("length(extraction_revision) > 0 AND length(location_sha256) = 64 AND length(span_sha256) = 64 AND start_byte >= 0 AND end_byte > start_byte", name="ck_document_evidence_identity"),
)

figure_bundle_evidence = Table(
    "figure_bundle_evidence", metadata,
    Column("bundle_id", BigInteger, ForeignKey("exact_figure_bundle.bundle_id", name="fk_bundle_evidence_bundle"), nullable=False),
    Column("evidence_key", Text, nullable=False),
    Column("evidence_id", BigInteger, ForeignKey("document_evidence.evidence_id", name="fk_bundle_evidence_document"), nullable=False),
    PrimaryKeyConstraint("bundle_id", "evidence_key", name="pk_figure_bundle_evidence"),
    CheckConstraint("length(evidence_key) > 0", name="ck_bundle_evidence_key"),
)

figure_activation_transition = Table(
    "figure_activation_transition", metadata,
    Column("transition_id", BigInteger, Identity(), nullable=False),
    Column("release_id", BigInteger, nullable=False),
    Column("parent_release_id", BigInteger),
    Column("security_id", BigInteger, nullable=False),
    Column("metric_path", Text, nullable=False),
    Column("view", Text, nullable=False),
    Column("period_kind", Text, nullable=False),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("previous_bundle_id", BigInteger),
    Column("previous_node_key", Text),
    Column("replacement_bundle_id", BigInteger),
    Column("replacement_node_key", Text),
    Column("value_text_changed", Boolean, nullable=False),
    Column("state_changed", Boolean, nullable=False),
    Column("provenance_changed", Boolean, nullable=False),
    Column("endpoint_presence_changed", Boolean, nullable=False),
    Column("declared_context_equal", Boolean, nullable=False),
    Column("context_assessment", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("contract_revision", Integer, nullable=False),
    PrimaryKeyConstraint("transition_id", name="pk_figure_activation_transition"),
    ForeignKeyConstraint(["release_id", "security_id"], ["release_company.release_id", "release_company.security_id"], name="fk_figure_transition_member"),
    ForeignKeyConstraint(["release_id", "parent_release_id"], ["data_release.release_id", "data_release.parent_release_id"], name="fk_figure_transition_parent"),
    ForeignKeyConstraint(["parent_release_id", "security_id", "previous_bundle_id"], ["release_company.release_id", "release_company.security_id", "release_company.exact_bundle_id"], name="fk_figure_transition_previous_member"),
    ForeignKeyConstraint(["release_id", "security_id", "replacement_bundle_id"], ["release_company.release_id", "release_company.security_id", "release_company.exact_bundle_id"], name="fk_figure_transition_replacement_member"),
    ForeignKeyConstraint(["previous_bundle_id", "previous_node_key"], ["figure_selection.bundle_id", "figure_selection.node_key"], name="fk_figure_transition_previous_node"),
    ForeignKeyConstraint(["replacement_bundle_id", "replacement_node_key"], ["figure_selection.bundle_id", "figure_selection.node_key"], name="fk_figure_transition_replacement_node"),
    CheckConstraint("(previous_bundle_id IS NULL) = (previous_node_key IS NULL) AND (replacement_bundle_id IS NULL) = (replacement_node_key IS NULL)", name="ck_figure_transition_endpoint_pairs"),
    CheckConstraint("(previous_bundle_id IS NOT NULL OR replacement_bundle_id IS NOT NULL) AND (parent_release_id IS NOT NULL OR previous_bundle_id IS NULL)", name="ck_figure_transition_endpoint_presence"),
    CheckConstraint("(period_kind = 'UNKNOWN' AND period_start IS NULL AND period_end IS NULL) OR (period_kind = 'INSTANT' AND period_start IS NULL AND period_end IS NOT NULL) OR (period_kind = 'DURATION' AND period_start IS NOT NULL AND period_end IS NOT NULL AND period_start <= period_end)", name="ck_figure_transition_period"),
    CheckConstraint("view IN ('STRICT','ASSUMED','CONSERVATIVE') AND length(metric_path) > 0", name="ck_figure_transition_identity"),
    CheckConstraint("context_assessment IN ('NOT_ASSESSED','INCOMPARABLE') AND length(reason) > 0 AND contract_revision = 1", name="ck_figure_transition_assessment"),
    Index("uq_figure_transition_output", "release_id", "security_id", "metric_path", "view", "period_kind", "period_start", "period_end", unique=True, postgresql_nulls_not_distinct=True),
)


def create_postgres_engine(settings: StorageSettings | None = None) -> Engine:
    return create_engine((settings or StorageSettings.from_env()).database_url)
