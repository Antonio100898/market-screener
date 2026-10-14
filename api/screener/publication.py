"""Release-backed snapshot selection; financial validation remains in the engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import BigInteger, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .durable_jobs import DurableJobRepository, LeaseToken
from .source_validity import accepted_witnesses, current_observation_id, insert_release_lineage
from .figure_history import FigureHistoryRepository
from .postgres import (
    active_data_release, company_snapshot, company_snapshot_artifact, current_company_snapshot, data_release,
    issuer, priced_security, publication_candidate, publication_candidate_source,
    release_candidate, release_company, source_item, source_observation, source_item_current,
)


class PublicationRequired(RuntimeError):
    pass


class UnknownDataRelease(ValueError):
    pass


class StalePublicationParent(RuntimeError):
    pass


class StalePublicationSource(RuntimeError):
    pass


class PublicationIdentityConflict(ValueError):
    pass


class PublicationSourceMismatch(ValueError):
    pass


@dataclass(frozen=True)
class DataReleaseRecord:
    release_id: int
    parent_release_id: int | None
    request_key: str
    publishing_job_id: int
    created_at: datetime
    published_at: datetime
    member_count: int
    projection_revision: int | None = None
    input_bundle_sha256: str | None = None
    figure_history_revision: int | None = None


@dataclass(frozen=True)
class RegisteredCandidate:
    candidate_id: int
    request_key: str
    security_id: int
    snapshot_id: int
    expected_parent_release_id: int | None
    cause_type: str
    registering_job_id: int
    registered_at: datetime
    source_observation_ids: tuple[int, ...]
    exact_bundle_id: int | None = None


def _require_publication_transaction(connection: Connection) -> None:
    if (not connection.in_transaction()
            or getattr(connection.connection.dbapi_connection, "autocommit", None) is not False):
        raise ValueError("publication requires a real transaction")


def lock_active_release(connection: Connection) -> int | None:
    connection.execute(
        insert(active_data_release).values(singleton_id=1).on_conflict_do_nothing()
    )
    return connection.scalar(
        select(active_data_release.c.release_id).where(
            active_data_release.c.singleton_id == 1
        ).with_for_update()
    )


def release_selection(release_id: int | None = None):
    """One statement chooses either published membership or pre-release bootstrap."""
    if release_id is not None:
        _id("release_id", release_id)
        return select(
            release_company.c.security_id, release_company.c.snapshot_id,
            release_company.c.release_id,
        ).where(release_company.c.release_id == release_id).subquery()
    active = select(
        release_company.c.security_id, release_company.c.snapshot_id,
        release_company.c.release_id,
    ).join(active_data_release, active_data_release.c.release_id == release_company.c.release_id)
    bootstrap = select(
        current_company_snapshot.c.security_id, current_company_snapshot.c.snapshot_id,
        literal(None, type_=BigInteger).label("release_id"),
    ).where(~exists(select(active_data_release.c.release_id).where(
        active_data_release.c.release_id.is_not(None)
    )))
    return active.union_all(bootstrap).subquery()


def require_release(connection: Connection, release_id: int | None) -> None:
    if release_id is not None and connection.scalar(select(data_release.c.release_id).where(
        data_release.c.release_id == _id("release_id", release_id),
        data_release.c.published_at.is_not(None),
    )) is None:
        raise UnknownDataRelease(f"published data release {release_id} does not exist")


class PublicationRepository:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), object_store=None):
        self.engine = engine
        self._clock = clock
        self._jobs = DurableJobRepository(engine)
        self._object_store = object_store

    def _prepare_primary_plans(self, entries, *, object_store=None):
        from .primary_plan_publication import PrimaryPlanPublicationPreparer
        owner = PrimaryPlanPublicationPreparer(self.engine,
            self._object_store if object_store is None else object_store)
        prepared = {}
        for snapshot_id, bundle_id, run_key in entries:
            key = (snapshot_id, bundle_id, run_key)
            if key not in prepared:
                prepared[key] = (owner, bundle_id, run_key, owner.prepare(*key))
        return prepared

    @staticmethod
    def _require_primary_plan(connection, snapshot_id, admission):
        owner, bundle_id, run_key, prepared = admission
        return owner.require_on_connection(connection, snapshot_id, bundle_id, run_key, prepared)

    def active(self) -> DataReleaseRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(data_release).join(
                active_data_release, active_data_release.c.release_id == data_release.c.release_id,
            )).mappings().one_or_none()
            return self._release_record(connection, row) if row else None

    def release(self, release_id: int) -> DataReleaseRecord:
        with self.engine.connect() as connection:
            require_release(connection, release_id)
            row = connection.execute(select(data_release).where(
                data_release.c.release_id == release_id,
            )).mappings().one()
            return self._release_record(connection, row)

    def retained_members(self, release_id: int) -> tuple[tuple[int, int, int | None], ...]:
        """Exact published membership associations; never the active default."""
        release_id = _id("release_id", release_id)
        with self.engine.connect() as connection:
            require_release(connection, release_id)
            return tuple(tuple(row) for row in connection.execute(select(
                release_company.c.security_id, release_company.c.snapshot_id, release_company.c.exact_bundle_id,
            ).where(release_company.c.release_id == release_id).order_by(release_company.c.security_id)))

    def candidate(self, candidate_id: int) -> RegisteredCandidate | None:
        """Read one immutable registered association; no activation qualification."""
        candidate_id = _id("candidate_id", candidate_id)
        with self.engine.connect() as connection:
            row = connection.execute(select(publication_candidate).where(
                publication_candidate.c.candidate_id == candidate_id,
                publication_candidate.c.registered_at.is_not(None),
            )).mappings().one_or_none()
            if row is None:
                return None
            return RegisteredCandidate(**row, source_observation_ids=self._candidate_sources(connection, candidate_id))

    def bootstrap(self, lease: LeaseToken, *, request_key: str) -> DataReleaseRecord:
        key = _key(request_key)
        with self.engine.begin() as connection:
            _require_publication_transaction(connection)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            parent = lock_active_release(connection)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            existing = self._existing_release(connection, key, None, ())
            if existing is not None:
                return existing
            if parent is not None:
                raise StalePublicationParent("initial release has already been published")
            memberships = connection.execute(select(
                current_company_snapshot.c.security_id, current_company_snapshot.c.snapshot_id,
            )).mappings().all()
            if not memberships:
                raise ValueError("initial release requires selected company snapshots")
            from .primary_plan_publication import PrimaryPlanPublicationPreparer
            for member in memberships:
                PrimaryPlanPublicationPreparer.identity_on_connection(connection, member["snapshot_id"], None, None)
                from .company_availability import ARTIFACT_ROLE, read_snapshot_info
                diagnostic = connection.scalar(select(company_snapshot_artifact.c.content_sha256).where(
                    company_snapshot_artifact.c.snapshot_id == member["snapshot_id"],
                    company_snapshot_artifact.c.role == ARTIFACT_ROLE))
                if diagnostic is not None:
                    assessment, _ = read_snapshot_info(connection, member["snapshot_id"])
                    sources = assessment.remote_observation_ids or tuple(connection.scalars(select(
                        source_observation.c.source_observation_id).join(source_item).where(
                        source_observation.c.artifact_sha256 == diagnostic,
                        source_item.c.source_system == assessment.issuer_source,
                        source_item.c.issuer_source_identifier == assessment.issuer_identifier)))
                    self._validate_sources(connection, sources, member["security_id"], member["snapshot_id"])
                adapter = connection.scalar(select(company_snapshot_artifact.c.content_sha256).where(
                    company_snapshot_artifact.c.snapshot_id == member["snapshot_id"],
                    company_snapshot_artifact.c.role == "canonical_company_adapter_facts"))
                if adapter is not None:
                    hashes = select(company_snapshot_artifact.c.content_sha256).where(
                        company_snapshot_artifact.c.snapshot_id == member["snapshot_id"])
                    sources = tuple(connection.scalars(select(source_observation.c.source_observation_id)
                        .join(source_item).join(source_item_current,
                            source_item_current.c.source_observation_id == source_observation.c.source_observation_id)
                        .where(source_observation.c.artifact_sha256.in_(hashes),
                               source_item.c.source_system == "COMPANY_ADAPTER",
                               source_item.c.issuer_source_identifier == "IFRS-ADIDAS-AG")))
                    sources = self._bound_dependencies(connection, sources, member["security_id"], member["snapshot_id"])
                    self._validate_sources(connection, sources, member["security_id"], member["snapshot_id"])
            return self._activate(connection, lease, key, None, memberships, ())

    def register_candidate(
        self, lease: LeaseToken, *, request_key: str, snapshot_id: int,
        expected_parent_release_id: int | None, source_observation_ids: Iterable[int],
        cause_type: str, exact_bundle_id: int | None = None, expected_exact_run_key: str | None = None,
    ) -> RegisteredCandidate:
        key = _key(request_key)
        snapshot_id = _id("snapshot_id", snapshot_id)
        parent = _parent_id(expected_parent_release_id)
        sources = _ids("source_observation_ids", source_observation_ids)
        if exact_bundle_id is None and expected_exact_run_key is not None:
            raise ValueError("expected_exact_run_key requires exact_bundle_id")
        if cause_type not in {"company_correction", "source_change", "extraction_fix", "calculation_change"}:
            raise ValueError("unsupported publication cause_type")
        admission = self._prepare_primary_plans(((snapshot_id, exact_bundle_id, expected_exact_run_key),))[
            (snapshot_id, exact_bundle_id, expected_exact_run_key)]
        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, self._clock)
            active = lock_active_release(connection) if parent is None else None
            self._jobs._lock_live_lease(connection, lease, self._clock)
            require_release(connection, parent)
            snapshot = connection.execute(select(company_snapshot).where(
                company_snapshot.c.snapshot_id == snapshot_id,
            )).mappings().one_or_none()
            if snapshot is None:
                raise ValueError("candidate snapshot does not exist")
            self._require_primary_plan(connection, snapshot_id, admission)
            binding = None
            captured_sources = ()
            if exact_bundle_id is not None:
                binding = FigureHistoryRepository.publication_identity_on_connection(connection, exact_bundle_id,
                    security_id=snapshot["security_id"], snapshot_id=snapshot_id, expected_run_key=expected_exact_run_key)
                previous = connection.execute(select(publication_candidate).where(
                    publication_candidate.c.request_key == key)).mappings().one_or_none()
                if previous is not None and previous["registered_at"] is not None:
                    captured_sources = self._candidate_sources(connection, previous["candidate_id"])
            sources = tuple(sorted(set(sources) | set(captured_sources))) if captured_sources else self._bound_dependencies(
                connection, sources, snapshot["security_id"], snapshot_id)
            if binding is not None:
                sources = tuple(sorted(set(sources) | set(binding["source_observation_ids"])))
            values = dict(request_key=key, security_id=snapshot["security_id"], snapshot_id=snapshot_id,
                          expected_parent_release_id=parent, cause_type=cause_type,
                          registering_job_id=lease.job_id, exact_bundle_id=exact_bundle_id)
            connection.execute(insert(publication_candidate).values(**values).on_conflict_do_nothing())
            row = connection.execute(select(publication_candidate).where(
                publication_candidate.c.request_key == key,
            )).mappings().one()
            for field in ("security_id", "snapshot_id", "expected_parent_release_id", "cause_type", "exact_bundle_id"):
                if row[field] != values[field]:
                    raise PublicationIdentityConflict("candidate request key identifies different inputs")
            if binding is not None:
                self._lock_source_items(connection, sources)
                self._jobs._lock_live_lease(connection, lease, self._clock)
                FigureHistoryRepository(self.engine).validate_publication_binding_on_connection(connection, exact_bundle_id,
                    security_id=snapshot["security_id"], snapshot_id=snapshot_id, expected_run_key=expected_exact_run_key)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            if row["registered_at"] is not None:
                recorded = self._candidate_sources(connection, row["candidate_id"])
                if recorded != sources:
                    raise PublicationIdentityConflict("candidate request key identifies different source revisions")
                return RegisteredCandidate(**row, source_observation_ids=recorded)
            if parent is None and active is not None:
                raise StalePublicationParent("initial candidate requires empty official state")
            self._validate_sources(connection, sources, row["security_id"], snapshot_id, primary_plan=admission)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            connection.execute(publication_candidate_source.insert(), [
                dict(candidate_id=row["candidate_id"], source_observation_id=source) for source in sources
            ])
            self._jobs._lock_live_lease(connection, lease, self._clock)
            row = connection.execute(update(publication_candidate).where(
                publication_candidate.c.candidate_id == row["candidate_id"],
            ).values(registered_at=self._clock()).returning(publication_candidate)).mappings().one()
            return RegisteredCandidate(**row, source_observation_ids=sources)

    def publish(
        self, lease: LeaseToken, *, request_key: str, candidate_ids: Iterable[int],
        expected_parent_release_id: int | None,
    ) -> DataReleaseRecord:
        return self._publish(lease, request_key=request_key, candidate_ids=candidate_ids,
                             expected_parent_release_id=expected_parent_release_id)

    def _publish(self, lease, *, request_key, candidate_ids, expected_parent_release_id, _display=None):
        key = _key(request_key)
        ids = tuple(sorted({_id("candidate_ids", value) for value in candidate_ids}))
        if not ids and _display is None:
            raise ValueError("candidate_ids must not be empty")
        parent = _parent_id(expected_parent_release_id)
        if parent is None and not ids:
            raise ValueError("initial publication candidate_ids must not be empty")
        with self.engine.connect() as connection:
            candidates = connection.execute(select(publication_candidate).where(
                publication_candidate.c.candidate_id.in_(ids))).mappings().all()
            entries = [(row["snapshot_id"], row["exact_bundle_id"],
                        None if row["exact_bundle_id"] is None else self._bundle_run(connection, row["exact_bundle_id"]))
                       for row in candidates]
        if _display is not None:
            entries.extend((entry["snapshot_id"], entry.get("exact_bundle_id"), entry.get("expected_exact_run_key"))
                           for entry in _display[1]["members"])
        admissions = self._prepare_primary_plans(entries,
            object_store=None if _display is None else _display[3].object_store)
        with self.engine.begin() as connection:
            _require_publication_transaction(connection)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            active = lock_active_release(connection)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            for (snapshot_id, _, _), admission in admissions.items():
                self._require_primary_plan(connection, snapshot_id, admission)
            existing = self._existing_release(connection, key, parent, ids, _display)
            if existing is not None:
                return existing
            if active != parent:
                raise StalePublicationParent("publication parent changed; rebuild and revalidate candidates")
            rows = connection.execute(select(publication_candidate).where(
                publication_candidate.c.candidate_id.in_(ids),
            ).order_by(publication_candidate.c.candidate_id)).mappings().all()
            if len(rows) != len(ids) or any(row["registered_at"] is None for row in rows):
                raise ValueError("all publication candidates must be registered")
            if any(row["expected_parent_release_id"] != parent for row in rows):
                raise StalePublicationParent("candidate was validated against a different parent")
            if connection.scalar(select(release_candidate.c.candidate_id).where(
                release_candidate.c.candidate_id.in_(ids),
            ).limit(1)) is not None:
                raise PublicationIdentityConflict("a candidate is already bound to a published release")
            if len({row["security_id"] for row in rows}) != len(rows):
                raise ValueError("a release may select only one candidate per security")
            # Sort the entire source set before locking so multi-company writers agree.
            sources = sorted({source for row in rows for source in self._candidate_sources(connection, row["candidate_id"])})
            bindings = {}
            for row in rows:
                if row["exact_bundle_id"] is not None:
                    identity = FigureHistoryRepository.publication_identity_on_connection(connection, row["exact_bundle_id"],
                        security_id=row["security_id"], snapshot_id=row["snapshot_id"],
                        expected_run_key=self._bundle_run(connection, row["exact_bundle_id"]))
                    if not set(identity["source_observation_ids"]).issubset(self._candidate_sources(connection, row["candidate_id"])):
                        raise PublicationSourceMismatch("candidate omits exact graph source dependencies")
                    bindings[row["candidate_id"]] = identity
                    sources = sorted(set(sources) | set(identity["source_observation_ids"]))
            if _display is not None:
                sources = sorted(set(sources) | {source for entry in _display[1]["members"] for source in entry["source_observation_ids"]})
                for entry in _display[1]["members"]:
                    if entry.get("exact_bundle_id") is not None:
                        identity = FigureHistoryRepository.publication_identity_on_connection(connection, entry["exact_bundle_id"],
                            security_id=entry["security_id"], snapshot_id=entry["snapshot_id"], expected_run_key=entry.get("expected_exact_run_key"))
                        sources = sorted(set(sources) | set(identity["source_observation_ids"]))
            self._lock_source_items(connection, sources)
            self._jobs._lock_live_lease(connection, lease, self._clock)
            for row in rows:
                plan_key = (row["snapshot_id"], row["exact_bundle_id"],
                    None if row["exact_bundle_id"] is None else bindings[row["candidate_id"]]["expected_exact_run_key"])
                self._validate_sources(connection, self._candidate_sources(connection, row["candidate_id"]),
                    row["security_id"], row["snapshot_id"], primary_plan=admissions.get(plan_key))
            changed = {row["security_id"]: dict(snapshot_id=row["snapshot_id"], exact_bundle_id=row["exact_bundle_id"]) for row in rows}
            memberships = [] if parent is None else connection.execute(select(release_company.c.security_id, release_company.c.snapshot_id, release_company.c.exact_bundle_id).where(
                release_company.c.release_id == parent,
            )).mappings().all()
            if _display is not None and _display[1]["contract_revision"] == 3:
                from .complete_accounting import load_accounting
                from .display_publication import canonical_bytes
                accounting = load_accounting(canonical_bytes(_display[1]["complete_accounting"]))
                accounting.validate_publication(parent, memberships, rows)
            merged = {row["security_id"]: dict(snapshot_id=row["snapshot_id"], exact_bundle_id=row["exact_bundle_id"]) for row in memberships}
            merged.update(changed)
            memberships = [dict(security_id=security, **binding) for security, binding in sorted(merged.items())]
            for row in rows:
                if row["exact_bundle_id"] is not None:
                    FigureHistoryRepository(self.engine).validate_publication_binding_on_connection(connection, row["exact_bundle_id"],
                        security_id=row["security_id"], snapshot_id=row["snapshot_id"], expected_run_key=bindings[row["candidate_id"]]["expected_exact_run_key"])
            if _display is not None:
                for entry in _display[1]["members"]:
                    declared = tuple(_id("source_observation_ids", value) for value in entry["source_observation_ids"])
                    if declared:
                        bound = self._bound_dependencies(connection, declared, entry["security_id"], entry["snapshot_id"])
                        if entry.get("exact_bundle_id") is not None:
                            identity = FigureHistoryRepository.publication_identity_on_connection(connection, entry["exact_bundle_id"],
                                security_id=entry["security_id"], snapshot_id=entry["snapshot_id"], expected_run_key=entry.get("expected_exact_run_key"))
                            bound = tuple(sorted(set(bound) | set(identity["source_observation_ids"])))
                        if bound != declared:
                            raise PublicationSourceMismatch("display manifest omits canonical source dependencies")
                        plan_key = (entry["snapshot_id"], entry.get("exact_bundle_id"), entry.get("expected_exact_run_key"))
                        self._validate_sources(connection, declared, entry["security_id"], entry["snapshot_id"],
                            primary_plan=admissions.get(plan_key))
                    elif entry["capture_mode"] != "regression_import":
                        raise PublicationSourceMismatch("captured display requires recorded canonical source lineage")
            return self._activate(connection, lease, key, parent, memberships, ids, _display)

    def publish_display(self, lease: LeaseToken, *, request_key: str, candidate_ids: Iterable[int] = (),
                        expected_parent_release_id: int | None, prepared, object_store) -> DataReleaseRecord:
        # S3 verification precedes the transaction and source/lease lock waits.
        from .display_publication import DisplayReleasePreparer
        preparer = DisplayReleasePreparer(self.engine, object_store)
        manifest, rows = preparer.verify(prepared)
        if manifest["contract_revision"] == 3:
            raise ValueError("complete display requires publish_complete_display")
        return self._publish(lease, request_key=request_key, candidate_ids=candidate_ids,
                            expected_parent_release_id=expected_parent_release_id,
                            _display=(prepared, manifest, rows, preparer))

    def publish_complete_display(self, lease: LeaseToken, *, request_key: str,
                                 expected_parent_release_id: int | None, prepared, object_store) -> DataReleaseRecord:
        from .complete_accounting import load_accounting
        from .display_publication import DisplayReleasePreparer, canonical_bytes
        preparer = DisplayReleasePreparer(self.engine, object_store)
        manifest, rows = preparer.verify(prepared)
        if manifest["contract_revision"] != 3:
            raise ValueError("complete publication requires complete display contract")
        accounting = load_accounting(canonical_bytes(manifest["complete_accounting"]))
        if _parent_id(expected_parent_release_id) != accounting.expected_parent_release_id:
            raise ValueError("complete accounting expected parent differs")
        return self._publish(lease, request_key=request_key,
            candidate_ids=[m.candidate_id for m in accounting.selected_members if m.candidate_id is not None],
            expected_parent_release_id=expected_parent_release_id, _display=(prepared, manifest, rows, preparer))

    def _activate(self, connection, lease, key, parent, memberships, candidate_ids, display=None):
        FigureHistoryRepository.require_membership_contract_on_connection(connection,
            [member["exact_bundle_id"] for member in memberships if member.get("exact_bundle_id") is not None])
        self._jobs._lock_live_lease(connection, lease, self._clock)
        display_values = {} if display is None else dict(projection_revision=display[0].projection_revision, input_bundle_sha256=display[0].input_bundle_sha256)
        row = connection.execute(data_release.insert().values(
            request_key=key, parent_release_id=parent, publishing_job_id=lease.job_id,
            figure_history_revision=1,
            **display_values,
        ).returning(data_release)).mappings().one()
        release_id = row["release_id"]
        connection.execute(release_company.insert(), [dict(release_id=release_id, **member) for member in memberships])
        if candidate_ids:
            connection.execute(release_candidate.insert(), [dict(release_id=release_id, candidate_id=candidate) for candidate in candidate_ids])
        if display is not None:
            display[3].insert(connection, release_id, memberships, display[1], display[2])
        candidate_sources = {}
        if candidate_ids:
            for candidate in connection.execute(select(publication_candidate).where(
                publication_candidate.c.candidate_id.in_(candidate_ids))).mappings():
                candidate_sources[candidate["security_id"]] = self._candidate_sources(connection, candidate["candidate_id"])
        validated = dict(candidate_sources)
        if display is not None:
            validated.update({entry["security_id"]: entry["source_observation_ids"] for entry in display[1]["members"]})
        witnesses = {}
        for member in memberships:
            security = member["security_id"]
            if security in validated:
                required = ()
                if member.get("exact_bundle_id") is not None:
                    identity = FigureHistoryRepository.publication_identity_on_connection(connection, member["exact_bundle_id"],
                        security_id=security, snapshot_id=member["snapshot_id"], expected_run_key=self._bundle_run(connection, member["exact_bundle_id"]))
                    required = identity["source_observation_ids"]
                witnesses[security] = accepted_witnesses(connection, validated[security], required_sources=required)
        insert_release_lineage(connection, release_id, memberships, parent_release_id=parent,
            changed_sources=candidate_sources, display_members=None if display is None else display[1]["members"], activation_witnesses=witnesses)
        FigureHistoryRepository(self.engine).record_activation_on_connection(connection,
            release_id=release_id, parent_release_id=parent, memberships=memberships, candidate_ids=candidate_ids)
        # Check time again after waiting for the publication/source locks.
        self._jobs._lock_live_lease(connection, lease, self._clock)
        now = self._clock()
        row = connection.execute(update(data_release).where(data_release.c.release_id == release_id).values(
            published_at=now,
        ).returning(data_release)).mappings().one()
        connection.execute(update(active_data_release).where(active_data_release.c.singleton_id == 1).values(release_id=release_id))
        self._jobs._record_item_outcome_on_connection(
            connection, lease, item_key=f"publication:{key}", status="succeeded",
            outcome={"release_id": release_id}, error_summary=None, now=self._clock, lease_is_locked=True,
        )
        return self._release_record(connection, row)

    @staticmethod
    def _bundle_run(connection, bundle_id):
        from .postgres import exact_figure_bundle
        return connection.scalar(select(exact_figure_bundle.c.pipeline_run_key).where(exact_figure_bundle.c.bundle_id == bundle_id))

    @staticmethod
    def _release_record(connection, row):
        count = connection.scalar(select(func.count()).select_from(release_company).where(release_company.c.release_id == row["release_id"]))
        return DataReleaseRecord(**row, member_count=count)

    @staticmethod
    def _candidate_sources(connection, candidate_id):
        return tuple(connection.scalars(select(publication_candidate_source.c.source_observation_id).where(
            publication_candidate_source.c.candidate_id == candidate_id,
        ).order_by(publication_candidate_source.c.source_observation_id)))

    def _existing_release(self, connection, key, parent, ids, display=None):
        row = connection.execute(select(data_release).where(data_release.c.request_key == key)).mappings().one_or_none()
        if row is None:
            return None
        recorded = tuple(connection.scalars(select(release_candidate.c.candidate_id).where(
            release_candidate.c.release_id == row["release_id"],
        ).order_by(release_candidate.c.candidate_id)))
        revision = None if display is None else display[0].projection_revision
        bundle = None if display is None else display[0].input_bundle_sha256
        if row["published_at"] is None or row["parent_release_id"] != parent or recorded != ids or row["projection_revision"] != revision or row["input_bundle_sha256"] != bundle:
            raise PublicationIdentityConflict("publication request key identifies different inputs")
        FigureHistoryRepository.require_release_contract_on_connection(connection, row["release_id"])
        return self._release_record(connection, row)

    @staticmethod
    def _lock_source_items(connection, sources):
        item_ids = select(source_observation.c.source_item_id).where(source_observation.c.source_observation_id.in_(sources))
        connection.execute(select(source_item.c.source_item_id).where(source_item.c.source_item_id.in_(item_ids)).order_by(source_item.c.source_item_id).with_for_update()).all()

    @staticmethod
    def _source_rows(connection, sources):
        return connection.execute(select(source_observation, source_item).join(source_item).where(
            source_observation.c.source_observation_id.in_(sources),
        )).mappings().all()

    def _bound_dependencies(self, connection, sources, security_id, snapshot_id):
        identity = connection.execute(select(issuer.c.source_system, issuer.c.source_identifier).join(
            priced_security, priced_security.c.issuer_id == issuer.c.issuer_id,
        ).where(priced_security.c.security_id == security_id)).mappings().one()
        hashes = select(company_snapshot_artifact.c.content_sha256).where(
            company_snapshot_artifact.c.snapshot_id == snapshot_id,
        )
        observations = connection.execute(select(source_observation, source_item).join(source_item).outerjoin(
            source_item_current, source_item_current.c.source_item_id == source_item.c.source_item_id).where(
            source_observation.c.artifact_sha256.in_(hashes),
            source_item.c.source_system == identity["source_system"],
            source_item.c.issuer_source_identifier == identity["source_identifier"],
            source_item.c.item_kind.in_(("resource", "aggregate")),
        ).order_by(source_item.c.source_item_id,
                   (source_observation.c.source_observation_id == source_item_current.c.source_observation_id).desc().nulls_last(),
                   source_observation.c.detected_at.desc(),
                   source_observation.c.source_observation_id.desc())).mappings().all()
        # One exact captured revision per input resource. A newer different hash
        # is still caught by the latest-source check before registration/activation.
        dependencies = {}
        for row in observations:
            dependencies.setdefault(row["source_item_id"], row)
        bound = set(sources)
        for row in dependencies.values():
            bound.add(row["source_observation_id"])
            if row["parent_source_item_id"] is not None:
                parent = row["canonical_metadata"].get("parent_observation_id")
                if isinstance(parent, bool) or not isinstance(parent, int) or parent <= 0:
                    raise PublicationSourceMismatch("retained input lacks an exact parent filing observation")
                bound.add(parent)
        return tuple(sorted(bound))

    def _validate_sources(self, connection, sources, security_id, snapshot_id, *, primary_plan=None):
        self._lock_source_items(connection, sources)
        identity = connection.execute(select(issuer.c.source_system, issuer.c.source_identifier).join(
            priced_security, priced_security.c.issuer_id == issuer.c.issuer_id,
        ).where(priced_security.c.security_id == security_id)).mappings().one()
        rows = self._source_rows(connection, sources)
        artifacts = connection.execute(select(company_snapshot_artifact).where(
            company_snapshot_artifact.c.snapshot_id == snapshot_id,
        )).mappings().all()
        from .company_availability import ARTIFACT_ROLE, read_snapshot_info, validate_source_rows
        if any(artifact["role"] == ARTIFACT_ROLE for artifact in artifacts):
            info = read_snapshot_info(connection, snapshot_id)
            if info is None or len(rows) != len(sources):
                raise PublicationSourceMismatch("unavailable candidate needs its actual recorded assessment")
            assessment, digest = info
            if (assessment.issuer_source != identity["source_system"]
                    or assessment.issuer_identifier != identity["source_identifier"]
                    or {a["content_sha256"] for a in artifacts} != {digest, *assessment.input_artifact_sha256s}):
                raise PublicationSourceMismatch("unavailable assessment identity or input custody differs")
            validate_source_rows(assessment, digest, rows)
            for row in rows:
                if row["source_observation_id"] != current_observation_id(connection, row["source_item_id"]):
                    raise StalePublicationSource("unavailable assessment was superseded; reassess")
            return
        if len(rows) != len(sources) or not any(row["item_kind"] == "filing" for row in rows):
            raise ValueError("candidate needs existing filing source observations")
        hashes = {artifact["content_sha256"] for artifact in artifacts}
        roles = {artifact["role"] for artifact in artifacts}
        covered_hashes = {row["artifact_sha256"] for row in rows}
        derived_roles = {"canonical_edinet_facts", "sec_inline_current_manifest"}
        adapter_roles = {"canonical_company_adapter_facts", "retained_company_adapter_facts",
                         "company_adapter_security_declaration"}
        if roles & adapter_roles:
            if (identity["source_system"] != "COMPANY_ADAPTER"
                    or identity["source_identifier"] != "IFRS-ADIDAS-AG"
                    or not {"canonical_company_adapter_facts", "raw_filing"} <= roles):
                raise PublicationSourceMismatch("generated adapter facts require their exact issuer and retained workbooks")
            if "company_adapter_security_declaration" in roles and "company_adapter_security_source" not in roles:
                raise PublicationSourceMismatch("adapter security declaration requires its retained official source")
            derived_roles |= adapter_roles
        plan_hash = None
        if "primary_input_plan" in roles:
            if primary_plan is None:
                raise PublicationSourceMismatch("internal primary plan requires verified producing pair preparation")
            plan_hash = self._require_primary_plan(connection, snapshot_id, primary_plan)
        for artifact in artifacts:
            if artifact["role"] == "primary_input_plan" and artifact["content_sha256"] == plan_hash:
                continue
            if artifact["role"] not in derived_roles and artifact["content_sha256"] not in covered_hashes:
                raise PublicationSourceMismatch("snapshot source input has no recorded observation lineage; bootstrap evidence needs reconciliation")
        if "canonical_edinet_facts" in roles and "raw_filing" not in roles:
            raise PublicationSourceMismatch("derived EDINET facts require retained source archives")
        if "sec_inline_current_manifest" in roles and not any(role.startswith("sec_inline_") and role not in derived_roles for role in roles):
            raise PublicationSourceMismatch("derived inline manifest requires retained source documents")
        if not artifacts:
            raise PublicationSourceMismatch("publication snapshot requires retained source input evidence")
        by_id = {row["source_observation_id"]: row for row in rows}
        linked_filings = set()
        for row in rows:
            if row["item_kind"] in {"resource", "aggregate"}:
                if row["artifact_sha256"] not in hashes:
                    raise PublicationSourceMismatch("source revision was not retained as a snapshot input")
                if row["parent_source_item_id"] is not None:
                    metadata = row["canonical_metadata"]
                    parent = by_id.get(metadata.get("parent_observation_id"))
                    if (parent is None or parent["item_kind"] != "filing"
                        or parent["source_item_id"] != row["parent_source_item_id"]
                        or metadata.get("parent_source_item_id") != row["parent_source_item_id"]
                        or metadata.get("parent_observation_sha256") != parent["observation_sha256"]):
                        raise PublicationSourceMismatch("snapshot input does not identify its exact parent filing revision")
                    linked_filings.add(parent["source_observation_id"])
            elif row["item_kind"] == "filing":
                continue
            elif row["artifact_sha256"] not in hashes:
                raise PublicationSourceMismatch("declared inventory was not retained as a snapshot input")
        for row in rows:
            if row["source_system"] != identity["source_system"] or row["issuer_source_identifier"] not in (None, identity["source_identifier"]):
                raise ValueError("candidate source identifies another issuer")
            if row["item_kind"] == "filing" and row["issuer_source_identifier"] != identity["source_identifier"]:
                raise ValueError("candidate filing must identify its issuer")
            if row["item_kind"] == "filing" and row["artifact_sha256"] not in hashes and row["source_observation_id"] not in linked_filings:
                raise PublicationSourceMismatch("filing revision has no retained document dependency in this snapshot")
            latest = current_observation_id(connection, row["source_item_id"])
            if row["source_observation_id"] != latest or row["state"] != "present":
                raise StalePublicationSource("candidate source revision changed or is not present; revalidate")


def _id(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parent_id(value):
    return None if value is None else _id("expected_parent_release_id", value)


def _ids(name, values):
    result = tuple(sorted({_id(name, value) for value in values}))
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _key(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("request_key must not be empty")
    return value.strip()
