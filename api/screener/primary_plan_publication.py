"""Verify internal plan declarations before acquiring publication locks."""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import secrets

from sqlalchemy import select

from .figure_history import FigureHistoryRepository
from .postgres import (company_snapshot, company_snapshot_artifact, durable_job,
                       exact_figure_bundle, figure_bundle_source, issuer, priced_security,
                       source_item_current, source_observation)
from .primary_input_transport import PLAN_ROLE, DEPENDENCY_ROLE
from .sec_ingestion import SecCandidateStageHandler, _candidate_parameters, _canonical_json


@dataclass(frozen=True)
class PreparedPrimaryPlan:
    identity: bytes
    plan_sha256: str
    verification_seal: bytes


class PrimaryPlanPublicationPreparer:
    def __init__(self, engine, object_store=None):
        self.engine = engine
        self.object_store = object_store
        self._preparation_key = secrets.token_bytes(32)

    @staticmethod
    def identity_on_connection(connection, snapshot_id, bundle_id, run_key):
        artifacts = tuple(sorted((row.content_sha256, row.role) for row in connection.execute(
            select(company_snapshot_artifact.c.content_sha256, company_snapshot_artifact.c.role)
            .where(company_snapshot_artifact.c.snapshot_id == snapshot_id))))
        plans = [digest for digest, role in artifacts if role == PLAN_ROLE]
        if not plans:
            return None
        if len(plans) != 1:
            raise ValueError("publication requires exactly one internal primary plan")
        snapshot = connection.execute(select(company_snapshot.c.snapshot_id,
            company_snapshot.c.security_id, company_snapshot.c.snapshot_sha256,
            company_snapshot.c.engine_revision, issuer.c.source_system, issuer.c.source_identifier)
            .join(priced_security, priced_security.c.security_id == company_snapshot.c.security_id)
            .join(issuer, issuer.c.issuer_id == priced_security.c.issuer_id)
            .where(company_snapshot.c.snapshot_id == snapshot_id)).mappings().one()
        binding = FigureHistoryRepository.publication_identity_on_connection(connection, bundle_id,
            security_id=snapshot["security_id"], snapshot_id=snapshot_id, expected_run_key=run_key)
        producer = connection.execute(select(durable_job.c.job_id, durable_job.c.parameters,
            durable_job.c.checkpoint).join(exact_figure_bundle,
            exact_figure_bundle.c.storing_job_id == durable_job.c.job_id)
            .where(exact_figure_bundle.c.bundle_id == bundle_id)).mappings().one()
        parameters, checkpoint = producer["parameters"], producer["checkpoint"]
        _candidate_parameters(parameters)
        if (parameters.get("primary_input_plan_sha256") != plans[0]
                or not isinstance(checkpoint, dict) or type(checkpoint.get("checkpoint_revision")) is not int
                or checkpoint["checkpoint_revision"] != 3):
            raise ValueError("internal primary plan requires its producing pair3 checkpoint")
        for field, value in (("snapshot_id", snapshot_id), ("security_id", snapshot["security_id"]),
                             ("exact_bundle_id", bundle_id), ("snapshot_sha256", snapshot["snapshot_sha256"]),
                             ("exact_bundle_sha256", binding["exact_bundle_sha256"]), ("pipeline_run_key", run_key)):
            if _canonical_json(checkpoint.get(field)) != _canonical_json(value):
                raise ValueError("internal primary plan producing pair identity differs")
        occurrences = [list(row) for row in connection.execute(select(
            figure_bundle_source.c.source_item_id, figure_bundle_source.c.source_observation_id,
            figure_bundle_source.c.occurrence_id).where(figure_bundle_source.c.bundle_id == bundle_id)
            .order_by(figure_bundle_source.c.source_item_id, figure_bundle_source.c.source_observation_id))]
        sources = set(binding["source_observation_ids"]) | {parameters["observation_id"]}
        accepted = [list(row) for row in connection.execute(select(
            source_item_current.c.source_item_id, source_item_current.c.source_observation_id,
            source_item_current.c.occurrence_id).where(source_item_current.c.source_item_id.in_(
                select(source_observation.c.source_item_id).where(
                    source_observation.c.source_observation_id.in_(sources))))
            .order_by(source_item_current.c.source_item_id))]
        return dict(snapshot=dict(snapshot), artifacts=artifacts, binding=binding,
                    producer=dict(producer), source_occurrences=occurrences,
                    accepted_occurrences=accepted, plan_sha256=plans[0])

    def prepare(self, snapshot_id, bundle_id, run_key):
        with self.engine.connect() as connection:
            identity = self.identity_on_connection(connection, snapshot_id, bundle_id, run_key)
        if identity is None:
            return None
        from .artifacts import SqlArtifactRepository
        from .shared_companies import SharedCompanyRepository
        from .source_observations import SourceObservationRepository
        objects, client = self.object_store, None
        if objects is None:
            from .object_store import ImmutableObjectStore, create_s3_client
            from .storage_config import StorageSettings
            settings = StorageSettings.from_env()
            client = create_s3_client(settings)
            objects = ImmutableObjectStore(client, settings.s3_bucket)
        try:
            stage = SecCandidateStageHandler(edgar=None, object_store=objects,
                artifacts=SqlArtifactRepository(self.engine), observations=SourceObservationRepository(self.engine),
                companies=SharedCompanyRepository(self.engine), history=FigureHistoryRepository(self.engine))
            from .durable_jobs import _job_record
            with self.engine.connect() as connection:
                producer = _job_record(connection.execute(select(durable_job).where(
                    durable_job.c.job_id == identity["producer"]["job_id"])).mappings().one())
            if _canonical_json(dict(job_id=producer.job_id, parameters=producer.parameters,
                                   checkpoint=producer.checkpoint)) != _canonical_json(identity["producer"]):
                raise ValueError("internal primary plan producer changed during preparation")
            stage.recover_paired_job(producer, producer.parameters["roots"])
            manifest = producer.checkpoint["primary_input_transport"]
            actual = {digest for digest, role in identity["artifacts"] if role == DEPENDENCY_ROLE}
            expected = {dependency["artifact_sha256"] for dependency in manifest["dependencies"]}
            if actual != expected:
                raise ValueError("internal primary plan dependency artifact set differs")
            body = _canonical_json(identity)
            return PreparedPrimaryPlan(body, identity["plan_sha256"],
                hmac.digest(self._preparation_key, body, "sha256"))
        finally:
            if client is not None:
                client.close()

    def require_on_connection(self, connection, snapshot_id, bundle_id, run_key, prepared):
        identity = self.identity_on_connection(connection, snapshot_id, bundle_id, run_key)
        if identity is None:
            if prepared is not None:
                raise ValueError("prepared internal primary plan no longer belongs to snapshot")
            return None
        body = _canonical_json(identity)
        if (not isinstance(prepared, PreparedPrimaryPlan) or prepared.identity != body
                or prepared.plan_sha256 != identity["plan_sha256"]
                or not hmac.compare_digest(prepared.verification_seal,
                    hmac.digest(self._preparation_key, body, "sha256"))):
            raise ValueError("internal primary plan requires verified preparation outside SQL")
        return prepared.plan_sha256
