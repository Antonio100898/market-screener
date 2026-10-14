"""Opt-in finite local SEC plans through existing immutable staging owners."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import re
import tempfile
import threading

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, ValidationInfo, model_validator
from sqlalchemy import text

from .artifacts import SqlArtifactRepository
from .captured_assembly import CapturedCanonicalPreparer, CapturedPreparationHandler
from .complete_input_plan import CompleteInputResolver, CompleteInputResolutionHandler
from .durable_jobs import DurableJobRepository, _utc_datetime
from .figure_history import FigureHistoryRepository, _json_field_adapters
from .job_runtime import DurableJobRuntime, ScheduledOccurrence
from .object_store import ImmutableObjectStore, create_s3_client
from .postgres import create_postgres_engine
from .publication import PublicationRepository
from .sec_ingestion import (
    SecCandidateStageHandler, SecQuarterlyReconciliationHandler,
    SecRecentDiscoveryHandler, SecResourceFetchHandler, _date_range, _year_quarter,
)
from .sec_inventory_plan import (KIND as FINITE_INVENTORY_KIND, PARTIAL_KIND as PARTIAL_INVENTORY_KIND, encode as inventory_encode,
    SecFiniteInventoryHandler, load as inventory_load, validate_parameters as inventory_parameters,
    validate_partial_parameters)
from .durable_jobs import _bounded_schedule_parameters, _bounded_schedule_text, _schedule_datetime
from .shared_companies import SharedCompanyRepository
from .source_observations import SourceObservationRepository
from .sources.edgar import EdgarClient
from .storage_config import AutomaticIngestionSettings, StorageSettings


PRODUCER_KINDS = frozenset({
    "sec-recent-discovery", "sec-recent-discovery-v2", "sec-recent-discovery-v3",
    "sec-index-reconciliation", "sec-index-reconciliation-v2", "sec-index-reconciliation-v3",
    "local-canonical-preparation", "local-complete-input-resolution",
    FINITE_INVENTORY_KIND, PARTIAL_INVENTORY_KIND,
})
SUPPORTED_SCHEMA_REVISIONS = frozenset({"20261004_0018", "20261004_0019", "20261004_0020", "20261009_0021"})
MAX_PLAN_BYTES = 1024 * 1024
MAX_SCHEDULE_KEY_BYTES = 1024


class PlanOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schedule_key: str
    scheduled_for: AwareDatetime
    kind: str
    parameters: dict
    due_at: AwareDatetime
    priority: StrictInt = Field(default=0, ge=-2147483648, le=2147483647)
    max_attempts: StrictInt = Field(default=3, ge=1, le=2147483647)
    deadline_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_request(self):
        if len(self.schedule_key.encode("utf-8")) > MAX_SCHEDULE_KEY_BYTES:
            raise ValueError("plan schedule key exceeds 1024 UTF-8 bytes")
        if self.kind not in PRODUCER_KINDS:
            raise ValueError("unsupported automatic ingestion producer kind")
        prefix = f"automatic-ingestion:v1:{self.kind}:"
        if "\0" in self.schedule_key or not self.schedule_key.startswith(prefix) or not self.schedule_key[len(prefix):].strip() or self.schedule_key != self.schedule_key.strip():
            raise ValueError("plan schedule key requires its versioned producer namespace")
        for name in ("scheduled_for", "due_at", "deadline_at"):
            value = getattr(self, name)
            if value is not None:
                try:
                    _utc_datetime(name, value)
                except OverflowError as error:
                    raise ValueError(f"{name} cannot be represented in UTC") from error
        if self.kind in {FINITE_INVENTORY_KIND, PARTIAL_INVENTORY_KIND}:
            finite = (validate_partial_parameters(self.parameters) if self.kind == PARTIAL_INVENTORY_KIND
                      else inventory_parameters(self.parameters))
            if (self.schedule_key != prefix + finite.operation_key
                    or self.scheduled_for != finite.operation_at):
                raise ValueError("finite inventory operation requires its exact scheduled key/time")
        elif self.kind.startswith("sec-recent-discovery"):
            _date_range(self.parameters)
        elif self.kind.startswith("sec-index-reconciliation"):
            _year_quarter(self.parameters)
        elif self.kind == "local-complete-input-resolution":
            if set(self.parameters) != {"complete_plan_sha256"} or type(self.parameters["complete_plan_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.parameters["complete_plan_sha256"]):
                raise ValueError("complete input resolution requires only its exact plan artifact SHA")
        elif set(self.parameters) != {"recipe_sha256"} or not isinstance(self.parameters["recipe_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", self.parameters["recipe_sha256"]):
            raise ValueError("canonical preparation requires only an exact recipe1 artifact SHA")
        if self.deadline_at is not None and self.deadline_at <= self.due_at:
            raise ValueError("deadline_at must be after due_at")
        return self


class FinitePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    plan_revision: StrictInt = Field(ge=1, le=1)
    occurrences: list[PlanOccurrence] = Field(min_length=1, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def partial_frontier(cls, value, info: ValidationInfo):
        if type(value) is dict and type(value.get("occurrences")) is list:
            if len(value["occurrences"]) > 1000:
                raise ValueError("automatic plan exceeds1000 occurrences before factories")
            for item in value["occurrences"]:
                if type(item) is dict and item.get("kind") == PARTIAL_INVENTORY_KIND:
                    _bounded_schedule_parameters(item)
                    parameters = item.get("parameters")
                    if type(parameters) is not dict or type(parameters.get("request_revision")) is not int or parameters["request_revision"] != 2:
                        raise ValueError("finite-v2 plan requires exact request_revision2 before factories")
                    operation = _bounded_schedule_text("finite operation key", parameters.get("operation_key"), 512)
                    key = _bounded_schedule_text("finite schedule key", item.get("schedule_key"), MAX_SCHEDULE_KEY_BYTES)
                    if key != f"automatic-ingestion:v1:{PARTIAL_INVENTORY_KIND}:" + operation:
                        raise ValueError("finite-v2 plan requires its exact namespace key before factories")
                    instants = {}
                    for name, raw in (("operation_at", parameters.get("operation_at")),
                                      ("scheduled_for", item.get("scheduled_for")), ("due_at", item.get("due_at")),
                                      ("deadline_at", item.get("deadline_at"))):
                        if name == "deadline_at" and raw is None:
                            continue
                        if type(raw) is not str or len(raw) > 64:
                            raise ValueError("finite-v2 plan time requires bounded aware text before factories")
                        instants[name] = _schedule_datetime(name, datetime.fromisoformat(raw))
                    if (instants["scheduled_for"] != instants["operation_at"]
                            or "deadline_at" in instants and instants["deadline_at"] <= instants["due_at"]):
                        raise ValueError("finite-v2 plan times differ before factories")
                    for name, default, minimum in (("priority", 0, -2**31), ("max_attempts", 3, 1)):
                        scalar = item.get(name, default)
                        if type(scalar) is not int or not minimum <= scalar < 2**31:
                            raise ValueError("finite-v2 plan schedule scalar requires exact signed32 before factories")
                    validate_partial_parameters(item.get("parameters"))
        if info.mode == "json" and type(value) is dict and "occurrences" in value:
            # Reuse strict JSON field admission after raw budgets; Python callers stay strict.
            value = {**value, "occurrences": _json_field_adapters(cls)["occurrences"].validate_json(
                inventory_encode(value["occurrences"]), strict=True)}
        return value

    @model_validator(mode="after")
    def unique_occurrences(self):
        identities = {(item.schedule_key, item.scheduled_for) for item in self.occurrences}
        if len(identities) != len(self.occurrences):
            raise ValueError("duplicate scheduled occurrence in finite plan")
        return self


def read_plan(path: Path) -> tuple[ScheduledOccurrence, ...]:
    with path.open("rb") as source:
        raw = source.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("automatic ingestion plan exceeds1MiB")
    plan = inventory_load(FinitePlan, raw)
    return tuple(ScheduledOccurrence(**item.model_dump()) for item in plan.occurrences)


def preflight(engine, s3, bucket: str) -> None:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision not in SUPPORTED_SCHEMA_REVISIONS:
            raise ValueError("automatic ingestion requires an explicitly supported migrated schema")
        connection.execute(text("SELECT checkpoint, ownership_generation FROM durable_job LIMIT 0"))
        connection.execute(text("SELECT occurrence_id FROM source_check_occurrence LIMIT 0"))
        connection.execute(text("SELECT bundle_id, snapshot_id FROM exact_figure_bundle LIMIT 0"))
    s3.head_bucket(Bucket=bucket)


class AutomaticIngestion:
    def __init__(self, settings: AutomaticIngestionSettings, *, engine=None, s3=None, edgar=None):
        settings.validate()
        if not settings.enabled:
            raise ValueError("automatic ingestion is disabled")
        self.settings = settings
        self.runtime = None
        self._engine, self._s3, self._edgar = engine, s3, edgar
        self._resources = ExitStack()
        self._closed = False

    def start(self) -> None:
        if self._closed or self.runtime is not None:
            raise RuntimeError("automatic ingestion assembly has already started or closed")
        occurrences = read_plan(self.settings.plan_path)
        storage = StorageSettings.from_env()
        if self._engine is None:
            self._engine = create_postgres_engine(storage)
            self._resources.callback(self._engine.dispose)
        if self._s3 is None:
            self._s3 = create_s3_client(storage)
            self._resources.callback(self._s3.close)
        preflight(self._engine, self._s3, storage.s3_bucket)
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.settings.cache_dir) as probe:
            probe.write(b"cache writable")
        if self._edgar is None:
            self._edgar = EdgarClient(cache_dir=self.settings.cache_dir)
            self._resources.callback(self._edgar.close)
        jobs = DurableJobRepository(self._engine)
        artifacts = SqlArtifactRepository(self._engine)
        observations = SourceObservationRepository(self._engine)
        companies = SharedCompanyRepository(self._engine)
        history = FigureHistoryRepository(self._engine)
        objects = ImmutableObjectStore(self._s3, storage.s3_bucket)
        producers = dict(edgar=self._edgar, object_store=objects, artifacts=artifacts,
                         observations=observations, jobs=jobs)
        canonical = CapturedCanonicalPreparer(companies, artifacts, objects, observations)
        stage_references = SecCandidateStageHandler(edgar=self._edgar, object_store=objects,
            artifacts=artifacts, observations=observations, companies=companies, history=history)
        handlers = {
            FINITE_INVENTORY_KIND: SecFiniteInventoryHandler(**producers, stage_references=stage_references),
            PARTIAL_INVENTORY_KIND: SecFiniteInventoryHandler(**producers, kind=PARTIAL_INVENTORY_KIND, stage_references=stage_references),
            "sec-recent-discovery": SecRecentDiscoveryHandler(**producers),
            "sec-recent-discovery-v2": SecRecentDiscoveryHandler(**producers, resource_kind="sec-resource-fetch-v2"),
            "sec-recent-discovery-v3": SecRecentDiscoveryHandler(**producers, resource_kind="sec-resource-fetch-v2", occurrence_bound=True),
            "sec-index-reconciliation": SecQuarterlyReconciliationHandler(**producers),
            "sec-index-reconciliation-v2": SecQuarterlyReconciliationHandler(**producers, resource_kind="sec-resource-fetch-v2"),
            "sec-index-reconciliation-v3": SecQuarterlyReconciliationHandler(**producers, resource_kind="sec-resource-fetch-v2", occurrence_bound=True),
            "sec-resource-fetch": SecResourceFetchHandler(**producers),
            "sec-resource-fetch-v2": SecResourceFetchHandler(**producers, annotate_annual=True),
            "sec-resource-fetch-v3": SecResourceFetchHandler(**producers, annotate_annual=True, retain_partial_acquisitions=True),
            "sec-candidate-stage": stage_references,
            "sec-retained-candidate-stage-v1": stage_references,
            "local-canonical-preparation": CapturedPreparationHandler(canonical),
            "local-complete-input-resolution": CompleteInputResolutionHandler(
                CompleteInputResolver(canonical, artifacts, objects, observations, companies, history, PublicationRepository(self._engine))),
        }
        self.runtime = DurableJobRuntime(jobs, lambda _now: occurrences, handlers, worker_count=1)
        if not self.runtime.start():
            raise RuntimeError("automatic ingestion runtime refused startup")

    def stop(self, timeout: float = 30) -> bool:
        if self.runtime is not None and not self.runtime.stop(timeout):
            return False
        self._resources.close()
        self._closed = True
        return True


class IngestionDrainError(RuntimeError):
    pass


_assembly_lock = threading.Lock()
_active: AutomaticIngestion | None = None


def start_automatic_ingestion(settings: AutomaticIngestionSettings | None = None, **borrowed) -> AutomaticIngestion | None:
    global _active
    current = settings or AutomaticIngestionSettings.from_env()
    current.validate()
    if not current.enabled:
        return None
    with _assembly_lock:
        if _active is not None:
            raise RuntimeError("an automatic ingestion assembly still owns its runtime/resources")
        assembly = AutomaticIngestion(current, **borrowed)
        _active = assembly
        try:
            assembly.start()
        except BaseException as error:
            if not assembly.stop():
                raise IngestionDrainError("automatic ingestion startup did not drain; resources remain owned") from error
            _active = None
            raise
        return assembly


def stop_automatic_ingestion(assembly: AutomaticIngestion | None) -> None:
    global _active
    if assembly is None:
        return
    with _assembly_lock:
        if _active is not assembly:
            raise RuntimeError("automatic ingestion stop does not own the active assembly")
        if not assembly.stop():
            raise IngestionDrainError("automatic ingestion did not drain; its resources remain owned")
        _active = None
