from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import store, sync
from .artifacts import ArtifactRepository, store_evidence
from .durable_jobs import DurableJobRepository, JobDeferred
from .evidence import EvidenceBundle
from .job_runtime import JobContext
from .object_store import ImmutableObjectStore
from .shared_companies import SharedCompanyRepository, SnapshotArtifact
from .source_observations import SourceItemIdentity, SourceObservationRepository
from .sources.edinet import EdinetClient, EdinetHttpError, validate_xbrl_archive
from .sources.edinet_mapper import build_edinet_companyfacts


_DOCUMENT_ID = re.compile(r"S[0-9A-Z]{7}")
_EDINET_CODE = re.compile(r"E[0-9]{5}")
_JST = ZoneInfo("Asia/Tokyo")
_EVENT_FLAGS = {
    ("withdrawalStatus", "1"): "withdrawal",
    ("docInfoEditStatus", "1"): "metadata_edit",
    ("disclosureStatus", "1"): "disclosure_stop",
    ("disclosureStatus", "3"): "disclosure_release",
}


class EdinetIngestionStopped(RuntimeError):
    pass


class EdinetResourcePending(JobDeferred):
    pass


class EdinetListDiscoveryHandler:
    def __init__(
        self,
        *,
        client: EdinetClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        jobs: DurableJobRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._jobs = jobs
        self._clock = clock

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        start, end = _date_range(context.job.parameters)
        through = _checkpoint_date(context.job.checkpoint, start, end)
        checkpoint = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        day = start
        while day <= end:
            if through is not None and day <= through:
                day += timedelta(days=1)
                continue
            if context.stopping.is_set():
                raise EdinetIngestionStopped("EDINET discovery stopped before next date")
            self._discover_day(context, day)
            checkpoint["through_date"] = day.isoformat()
            context.save_checkpoint(checkpoint)
            day += timedelta(days=1)
        if through is not None and "through_date" not in checkpoint:
            checkpoint["through_date"] = through.isoformat()
        return checkpoint

    def _discover_day(self, context: JobContext, day: date) -> None:
        index_date = day.isoformat()
        result = self._client.fetch_documents_on(index_date)
        artifact = store_evidence(
            result.data,
            result.media_type or "application/json",
            self._object_store,
            self._artifacts,
        )
        payload = _strict_json(result.data)
        process_text, process_at = _api_metadata(payload, index_date)
        aggregate = self._observations.record_edinet_list(
            context.lease,
            index_date=index_date,
            process_at=process_at,
            item_outcome_key=f"documents:{index_date}",
            source_url=result.url,
            artifact_sha256=artifact.content_sha256,
            etag=result.etag,
            last_modified=result.last_modified,
            detected_at=self._clock(),
        )
        rows = _rows(payload, index_date)
        prior = {
            stored.item.source_key: stored
            for stored in self._observations.latest_edinet_filings(index_date)
        }
        seen = set()
        for row in rows:
            if context.stopping.is_set():
                raise EdinetIngestionStopped("EDINET discovery stopped before next item")
            event = _event_kind(row)
            if event is not None:
                seen.add(row["docID"])
                if row.get("parentDocID"):
                    seen.add(row["parentDocID"])
                self._record_event(
                    context, aggregate, row, event, result.url,
                    index_date=index_date, process_text=process_text,
                    process_at=process_at,
                )
                continue
            document_id = row["docID"]
            if document_id in seen:
                raise ValueError("EDINET list repeats a filing document ID")
            seen.add(document_id)
            existing = self._observations.latest_item(
                "EDINET", "filing", document_id
            )
            withdrawal = self._observations.latest_edinet_event(
                document_id, ("withdrawal",)
            )
            disclosure = self._observations.latest_edinet_event(
                document_id, ("disclosure_stop", "disclosure_release")
            )
            edit = self._observations.latest_edinet_event(
                document_id, ("metadata_edit",)
            )
            effective_row = row
            issuer = (
                existing.item.issuer_source_identifier
                if existing is not None
                else row.get("edinetCode")
            )
            state, reason = _filing_state(row)
            if withdrawal is not None:
                state, reason = "removed", "withdrawn"
            if disclosure is not None and state != "removed":
                lifecycle_kind = disclosure.observation.canonical_metadata["event"]
                lifecycle_row = disclosure.observation.canonical_metadata["row"]
                if lifecycle_kind == "disclosure_stop":
                    state, reason = "unavailable", "not_disclosed"
                elif lifecycle_kind == "disclosure_release" and reason in {None, "not_disclosed"}:
                    state, reason = "present", "disclosure_restored"
                    effective_row = lifecycle_row
            if edit is not None:
                effective_row = edit.observation.canonical_metadata["row"]
                if state == "present" and reason is None:
                    reason = "metadata_edited"
            published_at = _published_at(effective_row)
            if state == "present" and published_at is None:
                raise ValueError("present EDINET filing has no submission timestamp")
            metadata = {
                "index_date": index_date,
                "process_at": process_text,
                "row": effective_row,
            }
            if reason is not None:
                metadata["reason"] = reason
                metadata["witness_observation_sha256"] = (
                    aggregate.observation.observation_sha256
                )
            same_state = (
                existing is not None
                and existing.observation.state == state
                and existing.observation.canonical_metadata.get("row") == effective_row
                and existing.observation.canonical_metadata.get("reason") == reason
            )
            if (
                existing is not None
                and _metadata_process_at(existing.observation.canonical_metadata)
                > process_at
            ):
                continue
            artifact_sha256 = artifact.content_sha256 if state == "present" else None
            if same_state:
                metadata = existing.observation.canonical_metadata
                artifact_sha256 = existing.observation.artifact_sha256
            stored = self._observations.record(
                context.lease,
                item=SourceItemIdentity(
                    "EDINET",
                    "filing",
                    document_id,
                    issuer_source_identifier=issuer,
                ),
                item_outcome_key=f"filing:{document_id}:{aggregate.observation.observation_sha256}",
                state=state,
                metadata=metadata,
                source_url=result.url,
                artifact_sha256=artifact_sha256,
                source_published_at=published_at,
                detected_at=self._clock(),
            )
            if state == "present" and _fetchable_annual(effective_row):
                self._enqueue_fetch(context, effective_row, stored, published_at)

        for document_id, stored in prior.items():
            if document_id in seen or stored.observation.state == "removed":
                continue
            if _metadata_process_at(stored.observation.canonical_metadata) > process_at:
                continue
            metadata = (
                stored.observation.canonical_metadata
                if stored.observation.state == "unavailable"
                and stored.observation.canonical_metadata.get("reason")
                == "absent_from_reconciled_list"
                else {
                    **stored.observation.canonical_metadata,
                    "reason": "absent_from_reconciled_list",
                    "witness_observation_sha256": (
                        aggregate.observation.observation_sha256
                    ),
                }
            )
            self._observations.record(
                context.lease,
                item=SourceItemIdentity(
                    "EDINET",
                    "filing",
                    document_id,
                    issuer_source_identifier=stored.item.issuer_source_identifier,
                ),
                item_outcome_key=(
                    f"missing:{document_id}:{aggregate.observation.observation_sha256}"
                ),
                state="unavailable",
                metadata=metadata,
                source_url=result.url,
                detected_at=self._clock(),
            )

    def _record_event(
        self, context, aggregate, row, event: str, source_url: str, *,
        index_date: str, process_text: str, process_at: datetime,
    ) -> None:
        document_id = row["docID"]
        sequence = row["seqNumber"]
        source_key = f"event/{index_date}/{sequence}"
        event_at = _event_at(row)
        assert event_at is not None
        metadata = {
            "event": event,
            "index_date": index_date,
            "process_at": event_at.isoformat(),
            "row": row,
        }
        existing = self._observations.latest_item("EDINET", "resource", source_key)
        artifact_sha256 = aggregate.observation.artifact_sha256
        if (
            existing is not None
            and existing.observation.canonical_metadata == metadata
        ):
            artifact_sha256 = existing.observation.artifact_sha256
        stored = self._observations.record(
            context.lease,
            item=SourceItemIdentity(
                "EDINET",
                "resource",
                source_key,
                issuer_source_identifier=row.get("edinetCode"),
                parent_source_item_id=aggregate.item.source_item_id,
            ),
            item_outcome_key=f"event:{index_date}:{sequence}",
            state="present",
            metadata=metadata,
            source_url=source_url,
            artifact_sha256=artifact_sha256,
            source_published_at=event_at,
            detected_at=self._clock(),
        )
        target_id = (
            row.get("parentDocID")
            if event == "withdrawal"
            else document_id
            if event in {"metadata_edit", "disclosure_stop", "disclosure_release"}
            else None
        )
        if target_id is None:
            return
        target = self._observations.latest_item(
            "EDINET", "filing", target_id
        )
        if target is None:
            return
        if _metadata_process_at(target.observation.canonical_metadata) > event_at:
            return
        if target.observation.state == "removed" and event != "withdrawal":
            return
        if (
            target.observation.canonical_metadata.get("reason")
            == "viewing_period_expired"
            and event != "withdrawal"
        ):
            return
        target_state = (
            "removed"
            if event == "withdrawal"
            else "unavailable"
            if event == "disclosure_stop"
            else target.observation.state
            if event == "metadata_edit"
            else "present"
        )
        reason = {
            "withdrawal": "withdrawn",
            "metadata_edit": (
                target.observation.canonical_metadata.get("reason")
                or "metadata_edited"
            ),
            "disclosure_stop": "not_disclosed",
            "disclosure_release": "disclosure_restored",
        }[event]
        target_metadata = {
            **target.observation.canonical_metadata,
            "process_at": event_at.isoformat(),
            "reason": reason,
            "event_observation_sha256": stored.observation.observation_sha256,
        }
        if event != "withdrawal":
            target_metadata["row"] = row
        target_artifact = (
            aggregate.observation.artifact_sha256
            if target_state == "present"
            else None
        )
        if (
            target.observation.state == target_state
            and target.observation.canonical_metadata.get("reason") == reason
            and target.observation.canonical_metadata.get("event_observation_sha256")
            == stored.observation.observation_sha256
            and target.observation.canonical_metadata.get("row")
            == target_metadata.get("row")
        ):
            target_metadata = target.observation.canonical_metadata
            target_artifact = target.observation.artifact_sha256
        target_stored = self._observations.record(
            context.lease,
            item=SourceItemIdentity(
                "EDINET",
                "filing",
                target.item.source_key,
                issuer_source_identifier=target.item.issuer_source_identifier,
            ),
            item_outcome_key=f"{event}:{target.item.source_key}:{stored.observation.observation_sha256}",
            state=target_state,
            metadata=target_metadata,
            source_url=source_url,
            artifact_sha256=target_artifact,
            detected_at=self._clock(),
        )
        if target_state == "present" and _fetchable_annual(row):
            self._enqueue_fetch(context, row, target_stored, _event_at(row))

    def _enqueue_fetch(self, context, row, stored, scheduled) -> None:
        assert scheduled is not None
        document_id = row["docID"]
        self._jobs.enqueue_child(
            context.lease,
            child_key=(
                f"edinet-resource-fetch:{document_id}:"
                f"{stored.observation.observation_sha256}"
            ),
            scheduled_for=scheduled,
            kind="edinet-resource-fetch",
            parameters={
                "document_id": document_id,
                "edinet_code": row["edinetCode"],
                "security_code": row["secCode"],
                "observation_id": stored.observation.source_observation_id,
            },
            due_at=scheduled,
            priority=context.job.priority,
            now=self._clock(),
        )


class EdinetResourceFetchHandler:
    def __init__(
        self,
        *,
        client: EdinetClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        companies: SharedCompanyRepository,
        listings: Mapping[str, Mapping[str, Any]],
        derive: Callable[[EvidenceBundle], tuple[str, dict | None]] = sync._derive_evidence,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._companies = companies
        if not isinstance(listings, Mapping):
            raise TypeError("EDINET listings must be a mapping")
        self._listings = listings
        self._derive = derive
        self._clock = clock

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        document_id, edinet_code, security_code, observation_id = (
            _resource_parameters(context.job.parameters)
        )
        completed = _resource_checkpoint(
            context.job.checkpoint,
            document_id=document_id,
            edinet_code=edinet_code,
            security_code=security_code,
            observation_id=observation_id,
        )
        if completed is not None and "snapshot_id" in completed:
            return completed

        parent = self._observations.latest_edinet_filing(observation_id)
        row = _validate_parent_filing(
            parent,
            document_id=document_id,
            edinet_code=edinet_code,
            security_code=security_code,
            observation_id=observation_id,
        )
        ticker = f"{security_code[:-1]}.T"
        listing = self._listings.get(security_code[:-1])
        if not isinstance(listing, Mapping):
            raise ValueError("EDINET security is not in the current JPX listing")
        if completed is None:
            archive, archive_artifact, archive_bytes = self._fetch_archive(
                context,
                parent=parent,
                document_id=document_id,
                edinet_code=edinet_code,
                security_code=security_code,
                observation_id=observation_id,
            )
            archive_checkpoint = {
                "document_id": document_id,
                "edinet_code": edinet_code,
                "security_code": security_code,
                "filing_observation_id": observation_id,
                "archive_observation_sha256": (
                    archive.observation.observation_sha256
                ),
            }
        else:
            archive_artifact, archive_bytes = self._load_archive(
                completed["archive_observation_sha256"],
                parent=parent,
                document_id=document_id,
                edinet_code=edinet_code,
                security_code=security_code,
                observation_id=observation_id,
            )
            archive_checkpoint = completed
        if context.stopping.is_set():
            raise EdinetIngestionStopped("EDINET archive fetch stopped before mapping")

        validate_xbrl_archive(archive_bytes, document_id)
        mapped = build_edinet_companyfacts(dict(row), archive_bytes, ticker=ticker)
        if completed is None:
            completed = archive_checkpoint
            context.save_checkpoint(completed)
        facts, prior_artifacts = self._merge_current(ticker, edinet_code, mapped)
        facts_artifact = store_evidence(
            _canonical_json(facts),
            "application/json",
            self._object_store,
            self._artifacts,
        )
        status, payload = self._derive(
            EvidenceBundle(edinet_code, ticker, facts, None, None)
        )
        if status != "ok" or payload is None:
            raise ValueError(
                f"EDINET filing evidence derived as {status}, not ok"
            )
        adapter = facts.get("_adapter") or {}
        reports = adapter.get("reports") or []
        if (
            adapter.get("kind") != "edinet_xbrl"
            or adapter.get("ticker") != ticker
            or adapter.get("security_code") != security_code
            or adapter.get("security_basis") != "PRIMARY_ORDINARY_SHARE"
            or not reports
        ):
            raise ValueError("EDINET mapped primary-security identity is invalid")
        latest_report = max(
            reports,
            key=lambda report: (
                str(report.get("published") or ""),
                str(report.get("document") or ""),
            ),
        )
        source_accession = latest_report.get("document")
        if not isinstance(source_accession, str) or _DOCUMENT_ID.fullmatch(
            source_accession
        ) is None:
            raise ValueError("EDINET mapped report identity is invalid")

        stored = self._companies.store_candidate(
            context.lease,
            issuer_source="EDINET",
            issuer_identifier=edinet_code,
            security_identifier=f"edinet:{edinet_code}:primary-ordinary",
            ticker=ticker,
            exchange_code="TSE",
            quote_currency=adapter.get("quote_currency"),
            security_title="PRIMARY_ORDINARY_SHARE",
            source_accession=source_accession,
            security_basis="PRIMARY_ORDINARY_SHARE",
            receipt_ratio=None,
            engine_revision=store.ENGINE_VERSION,
            canonical_payload=payload,
            artifacts=(
                *prior_artifacts,
                SnapshotArtifact(
                    archive_artifact.content_sha256, "raw_filing"
                ),
                SnapshotArtifact(
                    facts_artifact.content_sha256, "canonical_edinet_facts"
                ),
            ),
        )
        checkpoint = {
            **completed,
            "canonical_facts_sha256": facts_artifact.content_sha256,
            "snapshot_id": stored.snapshot_id,
            "snapshot_created": stored.created,
        }
        context.save_checkpoint(checkpoint)
        return checkpoint

    def _fetch_archive(
        self,
        context: JobContext,
        *,
        parent,
        document_id: str,
        edinet_code: str,
        security_code: str,
        observation_id: int,
    ):
        if context.stopping.is_set():
            raise EdinetIngestionStopped("EDINET archive fetch stopped before request")
        try:
            result = self._client.fetch_xbrl_archive(document_id)
        except EdinetHttpError as error:
            if error.status_code != 404:
                raise
            self._observations.record(
                context.lease,
                item=SourceItemIdentity(
                    "EDINET",
                    "resource",
                    f"{document_id}/xbrl",
                    issuer_source_identifier=edinet_code,
                    parent_source_item_id=parent.item.source_item_id,
                ),
                item_outcome_key=f"archive:{document_id}",
                state="pending",
                metadata={
                    **_archive_metadata(
                        parent,
                        document_id=document_id,
                        edinet_code=edinet_code,
                        security_code=security_code,
                        observation_id=observation_id,
                    ),
                    "reason": "not_found",
                },
                source_url=error.url,
                detected_at=self._clock(),
            )
            raise EdinetResourcePending("EDINET filing archive is pending") from error
        artifact = store_evidence(
            result.data,
            result.media_type or "application/zip",
            self._object_store,
            self._artifacts,
        )
        archive = self._observations.record(
            context.lease,
            item=SourceItemIdentity(
                "EDINET",
                "resource",
                f"{document_id}/xbrl",
                issuer_source_identifier=edinet_code,
                parent_source_item_id=parent.item.source_item_id,
            ),
            item_outcome_key=f"archive:{document_id}",
            state="present",
            metadata=_archive_metadata(
                parent,
                document_id=document_id,
                edinet_code=edinet_code,
                security_code=security_code,
                observation_id=observation_id,
            ),
            source_url=result.url,
            artifact_sha256=artifact.content_sha256,
            etag=result.etag,
            last_modified=result.last_modified,
            detected_at=self._clock(),
        )
        verified = self._object_store.read_verified(
            artifact.object_key, artifact.content_sha256, artifact.byte_size
        )
        return archive, artifact, verified

    def _load_archive(
        self,
        observation_sha256: str,
        *,
        parent,
        document_id: str,
        edinet_code: str,
        security_code: str,
        observation_id: int,
    ):
        stored = self._observations.by_sha256(observation_sha256)
        item = stored.item
        observation = stored.observation
        if (
            item.source_system != "EDINET"
            or item.item_kind != "resource"
            or item.source_key != f"{document_id}/xbrl"
            or item.issuer_source_identifier != edinet_code
            or item.parent_source_item_id != parent.item.source_item_id
            or observation.state != "present"
            or observation.artifact_sha256 is None
            or observation.canonical_metadata
            != _archive_metadata(
                parent,
                document_id=document_id,
                edinet_code=edinet_code,
                security_code=security_code,
                observation_id=observation_id,
            )
        ):
            raise ValueError("EDINET archive observation does not match its filing")
        artifact = self._artifacts.get(observation.artifact_sha256)
        payload = self._object_store.read_verified(
            artifact.object_key, artifact.content_sha256, artifact.byte_size
        )
        return artifact, payload

    def _merge_current(self, ticker: str, edinet_code: str, mapped: dict):
        current = self._companies.current_by_ticker(ticker)
        if current is None:
            return sync._merge_edinet_facts(None, mapped), ()
        if (
            current.issuer_source != "EDINET"
            or current.issuer_identifier != edinet_code
            or current.security_identifier
            != f"edinet:{edinet_code}:primary-ordinary"
        ):
            raise ValueError("current EDINET company identity conflicts with filing")
        canonical = [
            artifact
            for artifact in current.artifacts
            if artifact.role == "canonical_edinet_facts"
        ]
        if len(canonical) != 1:
            raise ValueError("current EDINET snapshot has no unique canonical facts")
        artifact = self._artifacts.get(canonical[0].content_sha256)
        previous = _strict_json(
            self._object_store.read_verified(
                artifact.object_key, artifact.content_sha256, artifact.byte_size
            )
        )
        adapter = previous.get("_adapter") or {}
        if (
            previous.get("cik") != edinet_code
            or adapter.get("kind") != "edinet_xbrl"
            or adapter.get("ticker") != ticker
        ):
            raise ValueError("current EDINET canonical facts identify another company")
        raw = tuple(
            artifact for artifact in current.artifacts if artifact.role == "raw_filing"
        )
        return sync._merge_edinet_facts(previous, mapped), raw


def _resource_parameters(
    parameters: Mapping[str, Any],
) -> tuple[str, str, str, int]:
    required = {"document_id", "edinet_code", "security_code", "observation_id"}
    if not isinstance(parameters, Mapping) or set(parameters) != required:
        raise ValueError(
            "EDINET resource fetch requires only document_id, edinet_code, "
            "security_code, and observation_id"
        )
    document_id = parameters["document_id"]
    edinet_code = parameters["edinet_code"]
    security_code = parameters["security_code"]
    observation_id = parameters["observation_id"]
    if not isinstance(document_id, str) or _DOCUMENT_ID.fullmatch(document_id) is None:
        raise ValueError("document_id must use the EDINET document format")
    if not isinstance(edinet_code, str) or _EDINET_CODE.fullmatch(edinet_code) is None:
        raise ValueError("edinet_code must use the EDINET filer format")
    if (
        not isinstance(security_code, str)
        or re.fullmatch(r"[0-9A-Z]{4}0", security_code) is None
    ):
        raise ValueError("security_code must be a five-character EDINET security code")
    if (
        isinstance(observation_id, bool)
        or not isinstance(observation_id, int)
        or observation_id <= 0
    ):
        raise ValueError("observation_id must be a positive integer")
    return document_id, edinet_code, security_code, observation_id


def _resource_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    document_id: str,
    edinet_code: str,
    security_code: str,
    observation_id: int,
) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    partial = {
        "document_id",
        "edinet_code",
        "security_code",
        "filing_observation_id",
        "archive_observation_sha256",
    }
    final = partial | {
        "canonical_facts_sha256", "snapshot_id", "snapshot_created"
    }
    if not isinstance(checkpoint, Mapping) or frozenset(checkpoint) not in {
        frozenset(partial), frozenset(final)
    }:
        raise ValueError("EDINET resource checkpoint has an invalid shape")
    invalid = (
        checkpoint["document_id"] != document_id
        or checkpoint["edinet_code"] != edinet_code
        or checkpoint["security_code"] != security_code
        or checkpoint["filing_observation_id"] != observation_id
        or not isinstance(checkpoint["archive_observation_sha256"], str)
        or re.fullmatch(
            r"[0-9a-f]{64}", checkpoint["archive_observation_sha256"]
        ) is None
    )
    if "snapshot_id" in checkpoint:
        invalid = invalid or (
            not isinstance(checkpoint["canonical_facts_sha256"], str)
            or re.fullmatch(
                r"[0-9a-f]{64}", checkpoint["canonical_facts_sha256"]
            ) is None
            or isinstance(checkpoint["snapshot_id"], bool)
            or not isinstance(checkpoint["snapshot_id"], int)
            or checkpoint["snapshot_id"] <= 0
            or not isinstance(checkpoint["snapshot_created"], bool)
        )
    if invalid:
        raise ValueError("EDINET resource checkpoint does not match its filing")
    return dict(checkpoint)


def _validate_parent_filing(
    parent,
    *,
    document_id: str,
    edinet_code: str,
    security_code: str,
    observation_id: int,
) -> Mapping[str, Any]:
    item = parent.item
    observation = parent.observation
    metadata = observation.canonical_metadata
    row = metadata.get("row") if isinstance(metadata, Mapping) else None
    if observation.source_observation_id != observation_id:
        raise ValueError("cited EDINET filing observation identity changed")
    if observation.state != "present":
        raise ValueError("cited EDINET filing observation is not present")
    if (
        item.source_system != "EDINET"
        or item.item_kind != "filing"
        or item.source_key != document_id
        or item.issuer_source_identifier != edinet_code
        or not isinstance(row, Mapping)
        or row.get("docID") != document_id
        or row.get("edinetCode") != edinet_code
        or row.get("secCode") != security_code
        or not _fetchable_annual(row)
    ):
        raise ValueError("cited EDINET filing observation does not match the job")
    return row


def _archive_metadata(
    parent,
    *,
    document_id: str,
    edinet_code: str,
    security_code: str,
    observation_id: int,
) -> dict[str, Any]:
    return {
        "role": "raw_filing",
        "document_id": document_id,
        "edinet_code": edinet_code,
        "security_code": security_code,
        "parent_source_item_id": parent.item.source_item_id,
        "parent_observation_id": observation_id,
        "parent_observation_sha256": parent.observation.observation_sha256,
    }


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _strict_json(data: bytes) -> dict[str, Any]:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("EDINET list contains duplicate object keys")
            out[key] = value
        return out

    try:
        payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("EDINET list is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("EDINET list must be a JSON object")
    return payload


def _api_metadata(
    payload: Mapping[str, Any], index_date: str
) -> tuple[str, datetime]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("status") != "200":
        raise ValueError("EDINET list metadata status is not successful")
    parameter = metadata.get("parameter")
    if (
        not isinstance(parameter, Mapping)
        or parameter.get("date") != index_date
        or parameter.get("type") != "2"
    ):
        raise ValueError("EDINET list metadata parameters do not match the request")
    process_text = metadata.get("processDateTime")
    process_at = _parse_jst(process_text)
    if process_at is None:
        raise ValueError("EDINET list process time is missing")
    return process_at.isoformat(), process_at


def _rows(payload: Mapping[str, Any], index_date: str) -> list[dict[str, Any]]:
    results = payload.get("results")
    metadata = payload.get("metadata") or {}
    resultset = metadata.get("resultset") or {}
    count = resultset.get("count")
    if (
        not isinstance(results, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(results)
    ):
        raise ValueError("EDINET list result count is invalid")
    rows = []
    sequences = set()
    required = {
        "seqNumber", "docID", "edinetCode", "secCode", "docTypeCode",
        "submitDateTime", "opeDateTime", "parentDocID", "withdrawalStatus",
        "docInfoEditStatus", "disclosureStatus", "legalStatus", "xbrlFlag",
    }
    for row in results:
        if not isinstance(row, dict) or not required <= set(row):
            raise ValueError("EDINET list contains a partial row")
        sequence = row["seqNumber"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or sequence in sequences
        ):
            raise ValueError("EDINET list contains an invalid sequence number")
        sequences.add(sequence)
        if _DOCUMENT_ID.fullmatch(str(row.get("docID") or "")) is None:
            raise ValueError("EDINET list contains an invalid document ID")
        if row.get("edinetCode") is not None and _EDINET_CODE.fullmatch(str(row["edinetCode"])) is None:
            raise ValueError("EDINET list contains an invalid filer code")
        for field, allowed in (
            ("withdrawalStatus", {"0", "1", "2"}),
            ("docInfoEditStatus", {"0", "1", "2"}),
            ("disclosureStatus", {"0", "1", "2", "3"}),
            ("legalStatus", {"0", "1", "2"}),
        ):
            if row.get(field) not in allowed:
                raise ValueError(f"EDINET list contains an invalid {field}")
        if row.get("xbrlFlag") not in {"0", "1"}:
            raise ValueError("EDINET list contains an invalid xbrlFlag")
        nonzero = sum(
            row[field] != "0"
            for field in (
                "withdrawalStatus", "docInfoEditStatus", "disclosureStatus"
            )
        )
        if nonzero > 1:
            raise ValueError("EDINET list row has ambiguous lifecycle states")
        event = _event_kind(row)
        parent = row.get("parentDocID")
        if parent is not None and _DOCUMENT_ID.fullmatch(str(parent)) is None:
            raise ValueError("EDINET list contains an invalid parent document ID")
        if event in {"metadata_edit", "disclosure_stop", "disclosure_release"} and not row.get("opeDateTime"):
            raise ValueError("EDINET operation event has no operation timestamp")
        if event is not None and _event_at(row) is None:
            raise ValueError("EDINET operation event has no timestamp")
        if event == "withdrawal" and parent is None:
            raise ValueError("EDINET withdrawal event has no parent document")
        rows.append(row)
    return rows


def _event_kind(row: Mapping[str, Any]) -> str | None:
    events = [event for (field, value), event in _EVENT_FLAGS.items() if row.get(field) == value]
    if len(events) > 1:
        raise ValueError("EDINET list row has ambiguous operation states")
    return events[0] if events else None


def _filing_state(row: Mapping[str, Any]) -> tuple[str, str | None]:
    if row["withdrawalStatus"] == "2":
        return "removed", "withdrawn"
    if row["disclosureStatus"] == "2":
        return "unavailable", "not_disclosed"
    if row["legalStatus"] == "0":
        return "unavailable", "viewing_period_expired"
    return "present", None


def _parse_jst(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M").replace(
            tzinfo=_JST
        ).astimezone(timezone.utc)
    except ValueError as error:
        raise ValueError("EDINET timestamp is invalid") from error


def _published_at(row: Mapping[str, Any]) -> datetime | None:
    return _parse_jst(row.get("submitDateTime"))


def _event_at(row: Mapping[str, Any]) -> datetime | None:
    if _event_kind(row) == "withdrawal":
        return _parse_jst(row.get("submitDateTime"))
    return _parse_jst(row.get("opeDateTime"))


def _metadata_process_at(metadata: Mapping[str, Any]) -> datetime:
    value = metadata.get("process_at")
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _fetchable_annual(row: Mapping[str, Any]) -> bool:
    security_code = row.get("secCode")
    return (
        row.get("docTypeCode") in {"120", "130"}
        and row.get("xbrlFlag") == "1"
        and bool(row.get("edinetCode"))
        and isinstance(security_code, str)
        and re.fullmatch(r"[0-9A-Z]{4}0", security_code) is not None
    )


def _date_range(parameters: Mapping[str, Any]) -> tuple[date, date]:
    if not isinstance(parameters, Mapping) or set(parameters) != {"start_date", "end_date"}:
        raise ValueError("EDINET discovery requires only start_date and end_date")
    try:
        start = date.fromisoformat(parameters["start_date"])
        end = date.fromisoformat(parameters["end_date"])
    except (TypeError, ValueError) as error:
        raise ValueError("EDINET discovery dates must be ISO dates") from error
    if end < start or (end - start).days > 31:
        raise ValueError("EDINET discovery range must be ordered and at most 31 days")
    return start, end


def _checkpoint_date(
    checkpoint: Mapping[str, Any] | None, start: date, end: date
) -> date | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "start_date", "end_date", "through_date"
    }:
        raise ValueError("EDINET discovery checkpoint has an invalid shape")
    if checkpoint["start_date"] != start.isoformat() or checkpoint["end_date"] != end.isoformat():
        raise ValueError("EDINET discovery checkpoint does not match its range")
    through = date.fromisoformat(checkpoint["through_date"])
    if through < start or through > end:
        raise ValueError("EDINET discovery checkpoint is outside its range")
    return through
