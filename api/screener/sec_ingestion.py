from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .artifacts import ArtifactRepository, store_evidence
from .durable_jobs import DurableJobRepository, JobDeferred
from .job_runtime import JobContext
from .normalize import FINANCIAL_FORMS
from .object_store import ImmutableObjectStore
from .source_observations import SourceItemIdentity, SourceObservationRepository
from .sources.edgar import EdgarClient, EdgarError, NoXbrlDataError


_FORM_INDEX_HEADER = (
    "Form Type   Company Name                                                  "
    "CIK         Date Filed  File Name"
)
_COLUMN_STARTS = (0, 12, 74, 86, 98)
_SEPARATOR = re.compile(r"^-{10}  -{60}  -{10}  -{10}  -{9,}$")
_ARCHIVE_FILE = re.compile(
    r"^edgar/data/(?P<cik>[0-9]+)/"
    r"(?P<accession>[0-9]{10}-[0-9]{2}-[0-9]{6})\.txt$"
)
_SEC_FINANCIAL_PREFIXES = tuple(
    form
    for form in FINANCIAL_FORMS
    if form in {"10-K", "10-Q", "20-F", "40-F", "6-K"}
)
_DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/"
    "{year}/QTR{quarter}/form.{ymd}.idx"
)
_QUARTERLY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/"
    "{year}/QTR{quarter}/form.idx"
)
_ACCESSION_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
)
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_COMPANY_FACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
)
_SUPPORTED_SEC_FORM = re.compile(
    r"^(?:10-[KQ]T?|20-F|40-F|6-K)(?:/A)?$"
)
_MAX_DAYS = 31
_FILING_COMPARISON_FIELDS = (
    "form",
    "company_name",
    "cik",
    "filing_date",
    "archive_filename",
    "accession",
    "quarter",
)


@dataclass(frozen=True)
class SecIndexFiling:
    form: str
    company_name: str
    cik: str
    filing_date: date
    archive_filename: str
    accession: str
    source_row: str


class SecDiscoveryStopped(RuntimeError):
    pass


class SecResourcePending(JobDeferred):
    pass


@dataclass(frozen=True)
class _SecResource:
    role: str
    item_kind: str
    source_key: str
    url: str
    parented: bool
    validator: Callable[[bytes, str, str, str, str], None] | None = None


def parse_form_index(data: bytes) -> tuple[SecIndexFiling, ...]:
    """Parse one SEC daily fixed-width form index, failing on shape drift."""
    if not isinstance(data, bytes):
        raise ValueError("SEC form index must be bytes")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("SEC form index must be ASCII") from error

    header_rows = [index for index, line in enumerate(lines) if line == _FORM_INDEX_HEADER]
    if len(header_rows) != 1:
        raise ValueError("SEC form index header shape changed")
    header_row = header_rows[0]
    if header_row + 1 >= len(lines) or _SEPARATOR.fullmatch(lines[header_row + 1]) is None:
        raise ValueError("SEC form index separator shape changed")

    filings = []
    for row in lines[header_row + 2 :]:
        if not row.strip():
            continue
        if len(row) < _COLUMN_STARTS[-1] + 1:
            raise ValueError("SEC form index contains a short data row")
        fields = tuple(
            row[start:end].strip()
            for start, end in zip(_COLUMN_STARTS, _COLUMN_STARTS[1:] + (None,))
        )
        form, company_name, cik_value, filed_value, archive_filename = fields
        if not form or not company_name:
            raise ValueError("SEC form index contains an empty form or company name")
        if re.fullmatch(r"[0-9]{1,10}", cik_value) is None:
            raise ValueError("SEC form index contains an invalid CIK")
        try:
            filing_date = date.fromisoformat(filed_value)
        except ValueError as error:
            raise ValueError("SEC form index contains an invalid filing date") from error
        match = _ARCHIVE_FILE.fullmatch(archive_filename)
        if match is None or int(match.group("cik")) != int(cik_value):
            raise ValueError("SEC form index contains an invalid archive filename")
        if not form.startswith(_SEC_FINANCIAL_PREFIXES):
            continue
        filings.append(
            SecIndexFiling(
                form=form,
                company_name=company_name,
                cik=f"{int(cik_value):010d}",
                filing_date=filing_date,
                archive_filename=archive_filename,
                accession=match.group("accession"),
                source_row=row,
            )
        )
    return tuple(filings)


class SecRecentDiscoveryHandler:
    def __init__(
        self,
        *,
        edgar: EdgarClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        jobs: DurableJobRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._edgar = edgar
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._jobs = jobs
        self._clock = clock

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        start, end = _date_range(context.job.parameters)
        through = _checkpoint_date(context.job.checkpoint, start, end)
        day = start
        checkpoint: dict[str, Any] = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        while day <= end:
            if through is not None and day <= through:
                day += timedelta(days=1)
                continue
            if context.stopping.is_set():
                raise SecDiscoveryStopped("SEC discovery stopped before the next date")
            self._discover_day(context, day)
            checkpoint["through_date"] = day.isoformat()
            context.save_checkpoint(checkpoint)
            day += timedelta(days=1)
        if through is not None and "through_date" not in checkpoint:
            checkpoint["through_date"] = through.isoformat()
        return checkpoint

    def _discover_day(self, context: JobContext, day: date) -> None:
        quarter = (day.month - 1) // 3 + 1
        url = _DAILY_INDEX_URL.format(
            year=day.year,
            quarter=quarter,
            ymd=day.strftime("%Y%m%d"),
        )
        inventory_key = f"daily-index/{day.isoformat()}"
        outcome_key = f"daily-index:{day.isoformat()}"
        inventory_metadata = {
            "index_date": day.isoformat(),
            "quarter": f"{day.year}Q{quarter}",
        }
        try:
            result = self._edgar.fetch(url)
        except NoXbrlDataError:
            self._observations.record(
                context.lease,
                item=SourceItemIdentity("SEC", "inventory", inventory_key),
                item_outcome_key=outcome_key,
                state="unavailable",
                metadata={**inventory_metadata, "reason": "not_found"},
                source_url=url,
                detected_at=self._clock(),
            )
            return
        except EdgarError:
            raise

        artifact = store_evidence(
            result.data,
            result.media_type or "application/octet-stream",
            self._object_store,
            self._artifacts,
        )
        inventory = self._observations.record(
            context.lease,
            item=SourceItemIdentity("SEC", "inventory", inventory_key),
            item_outcome_key=outcome_key,
            state="present",
            metadata=inventory_metadata,
            source_url=result.url,
            artifact_sha256=artifact.content_sha256,
            etag=result.etag,
            last_modified=result.last_modified,
            detected_at=self._clock(),
        )
        filings = parse_form_index(result.data)
        for filing in filings:
            stored = self._observations.record(
                context.lease,
                item=SourceItemIdentity(
                    "SEC",
                    "filing",
                    filing.accession,
                    issuer_source_identifier=filing.cik,
                ),
                item_outcome_key=(
                    f"filing:{filing.accession}:"
                    f"{inventory.observation.observation_sha256}"
                ),
                state="present",
                metadata={
                    "form": filing.form,
                    "company_name": filing.company_name,
                    "cik": filing.cik,
                    "filing_date": filing.filing_date.isoformat(),
                    "archive_filename": filing.archive_filename,
                    "accession": filing.accession,
                    "source_row": filing.source_row,
                    "index_date": day.isoformat(),
                    "quarter": f"{day.year}Q{quarter}",
                },
                source_url=result.url,
                artifact_sha256=artifact.content_sha256,
                etag=result.etag,
                last_modified=result.last_modified,
                detected_at=self._clock(),
            )
            observation = stored.observation
            immediate = datetime.combine(
                filing.filing_date,
                time.min,
                tzinfo=timezone.utc,
            )
            self._jobs.enqueue_child(
                context.lease,
                child_key=(
                    f"sec-resource-fetch:{filing.cik}:{filing.accession}:"
                    f"{observation.observation_sha256}"
                ),
                scheduled_for=immediate,
                kind="sec-resource-fetch",
                parameters={
                    "accession": filing.accession,
                    "cik": filing.cik,
                    "form": filing.form,
                    "filename": filing.archive_filename,
                    "observation_id": observation.source_observation_id,
                },
                due_at=immediate,
                priority=context.job.priority,
                now=self._clock(),
            )


class SecQuarterlyReconciliationHandler:
    def __init__(
        self,
        *,
        edgar: EdgarClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        jobs: DurableJobRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._edgar = edgar
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._jobs = jobs
        self._clock = clock

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        year, quarter = _year_quarter(context.job.parameters)
        completed = _quarter_checkpoint(context.job.checkpoint, year, quarter)
        if completed is not None:
            return completed
        self._check_stopping(context)
        partition = f"{year}Q{quarter}"
        url = _QUARTERLY_INDEX_URL.format(year=year, quarter=quarter)
        inventory_key = f"quarterly-index/{partition}"
        inventory_outcome_key = f"quarterly-index:{year}:Q{quarter}"
        try:
            result = self._edgar.fetch(url)
        except NoXbrlDataError:
            self._observations.record(
                context.lease,
                item=SourceItemIdentity("SEC", "inventory", inventory_key),
                item_outcome_key=inventory_outcome_key,
                state="unavailable",
                metadata={"quarter": partition, "reason": "not_found"},
                source_url=url,
                detected_at=self._clock(),
            )
            raise

        artifact = store_evidence(
            result.data,
            result.media_type or "application/octet-stream",
            self._object_store,
            self._artifacts,
        )
        filings = _filings_by_accession(parse_form_index(result.data))
        self._check_stopping(context)
        inventory = self._observations.record(
            context.lease,
            item=SourceItemIdentity("SEC", "inventory", inventory_key),
            item_outcome_key=inventory_outcome_key,
            state="present",
            metadata={"quarter": partition},
            source_url=result.url,
            artifact_sha256=artifact.content_sha256,
            etag=result.etag,
            last_modified=result.last_modified,
            detected_at=self._clock(),
        )
        latest = {
            row.canonical_metadata["accession"]: row
            for row in self._observations.latest_sec_financial_filings(partition)
        }

        for accession, filing in filings.items():
            self._check_stopping(context)
            prior = latest.get(accession)
            comparison = _filing_comparison(filing, partition)
            outcome_key = _quarterly_filing_outcome_key(
                accession,
                inventory.observation.observation_sha256,
            )
            if (
                prior is not None
                and prior.state == "present"
                and _stored_comparison(prior.canonical_metadata) == comparison
            ):
                context.record_item_outcome(
                    item_key=outcome_key,
                    status="succeeded",
                    outcome=_checked_outcome(
                        action="unchanged",
                        filing=prior,
                        inventory=inventory,
                    ),
                )
                if prior.detecting_job_id == context.job.job_id:
                    self._enqueue_resource(context, filing, prior)
                continue

            stored = self._observations.record(
                context.lease,
                item=SourceItemIdentity(
                    "SEC",
                    "filing",
                    filing.accession,
                    issuer_source_identifier=filing.cik,
                ),
                item_outcome_key=outcome_key,
                state="present",
                metadata={**comparison, "source_row": filing.source_row},
                source_url=result.url,
                artifact_sha256=artifact.content_sha256,
                etag=result.etag,
                last_modified=result.last_modified,
                detected_at=self._clock(),
            )
            self._enqueue_resource(context, filing, stored.observation)

        for accession, prior in latest.items():
            if accession in filings:
                continue
            self._check_stopping(context)
            outcome_key = _quarterly_filing_outcome_key(
                accession,
                inventory.observation.observation_sha256,
            )
            if prior.state != "present":
                context.record_item_outcome(
                    item_key=outcome_key,
                    status="succeeded",
                    outcome=_checked_outcome(
                        action=(
                            "already_removed"
                            if prior.state == "removed"
                            else "not_present"
                        ),
                        filing=prior,
                        inventory=inventory,
                    ),
                )
                continue
            self._observations.record_witnessed_sec_removal(
                context.lease,
                prior_observation_id=prior.source_observation_id,
                item_outcome_key=outcome_key,
                quarter=partition,
                inventory_source_item_id=inventory.item.source_item_id,
                inventory_observation_id=(
                    inventory.observation.source_observation_id
                ),
                inventory_observation_sha256=(
                    inventory.observation.observation_sha256
                ),
                inventory_artifact_sha256=artifact.content_sha256,
                detected_at=self._clock(),
            )

        self._check_stopping(context)
        checkpoint = {
            "year": year,
            "quarter": quarter,
            "inventory_observation_id": (
                inventory.observation.source_observation_id
            ),
            "inventory_observation_sha256": (
                inventory.observation.observation_sha256
            ),
        }
        context.save_checkpoint(checkpoint)
        return checkpoint

    def _enqueue_resource(self, context, filing, observation) -> None:
        immediate = datetime.combine(
            filing.filing_date,
            time.min,
            tzinfo=timezone.utc,
        )
        self._jobs.enqueue_child(
            context.lease,
            child_key=(
                f"sec-resource-fetch:{filing.cik}:{filing.accession}:"
                f"{observation.observation_sha256}"
            ),
            scheduled_for=immediate,
            kind="sec-resource-fetch",
            parameters={
                "accession": filing.accession,
                "cik": filing.cik,
                "form": filing.form,
                "filename": filing.archive_filename,
                "observation_id": observation.source_observation_id,
            },
            due_at=immediate,
            priority=context.job.priority,
            now=self._clock(),
        )

    @staticmethod
    def _check_stopping(context: JobContext) -> None:
        if context.stopping.is_set():
            raise SecDiscoveryStopped("SEC reconciliation stopped before new work")


class SecResourceFetchHandler:
    def __init__(
        self,
        *,
        edgar: EdgarClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._edgar = edgar
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._clock = clock

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        accession, cik, form, filename, observation_id = _resource_parameters(
            context.job.parameters
        )
        completed = _resource_checkpoint(
            context.job.checkpoint,
            accession=accession,
            cik=cik,
            observation_id=observation_id,
        )
        if completed is not None:
            return completed

        parent = self._observations.latest_sec_filing(observation_id)
        _validate_parent_filing(
            parent,
            accession=accession,
            cik=cik,
            form=form,
            filename=filename,
            observation_id=observation_id,
        )
        resources = _root_resources(
            accession=accession,
            cik=cik,
            filename=filename,
        )
        roots: dict[str, str] = {}
        filing_date = parent.observation.canonical_metadata.get("filing_date", "")
        for resource in resources:
            if context.stopping.is_set():
                raise SecDiscoveryStopped(
                    "SEC resource fetch stopped before the next resource"
                )
            metadata = _resource_metadata(
                resource=resource,
                role=resource.role,
                accession=accession,
                cik=cik,
                form=form,
                filename=filename,
                parent=parent,
            )
            try:
                result = self._edgar.fetch(resource.url)
            except NoXbrlDataError as error:
                self._observations.record(
                    context.lease,
                    item=_resource_identity(resource, cik=cik, parent=parent),
                    item_outcome_key=f"root:{resource.role}",
                    state="pending",
                    metadata={**metadata, "reason": "not_found"},
                    source_url=resource.url,
                    detected_at=self._clock(),
                )
                raise SecResourcePending(
                    f"SEC root resource is pending: {resource.role}"
                ) from error

            artifact = store_evidence(
                result.data,
                result.media_type or "application/octet-stream",
                self._object_store,
                self._artifacts,
            )
            stored = self._observations.record(
                context.lease,
                item=_resource_identity(resource, cik=cik, parent=parent),
                item_outcome_key=f"root:{resource.role}",
                state="present",
                metadata=metadata,
                source_url=result.url,
                artifact_sha256=artifact.content_sha256,
                etag=result.etag,
                last_modified=result.last_modified,
                detected_at=self._clock(),
            )
            if resource.validator is not None:
                try:
                    resource.validator(
                        result.data, accession, cik, form, filing_date
                    )
                except SecResourcePending as error:
                    context.record_item_outcome(
                        item_key=f"root:{resource.role}",
                        status="failed",
                        outcome={
                            "state": "pending",
                            "source_item_id": stored.item.source_item_id,
                            "source_observation_id": (
                                stored.observation.source_observation_id
                            ),
                            "observation_sha256": (
                                stored.observation.observation_sha256
                            ),
                        },
                        error_summary=str(error),
                    )
                    raise
            roots[resource.role] = stored.observation.observation_sha256

        checkpoint = {
            "accession": accession,
            "cik": cik,
            "filing_observation_id": observation_id,
            "roots": roots,
        }
        context.save_checkpoint(checkpoint)
        return checkpoint


def _resource_parameters(
    parameters: Mapping[str, Any],
) -> tuple[str, str, str, str, int]:
    required = {"accession", "cik", "form", "filename", "observation_id"}
    if not isinstance(parameters, Mapping) or set(parameters) != required:
        raise ValueError(
            "SEC resource fetch requires only accession, cik, form, filename, "
            "and observation_id"
        )
    accession = parameters["accession"]
    cik = parameters["cik"]
    form = parameters["form"]
    filename = parameters["filename"]
    observation_id = parameters["observation_id"]
    if not isinstance(accession, str) or re.fullmatch(
        r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession
    ) is None:
        raise ValueError("accession must use the SEC accession format")
    if (
        not isinstance(cik, str)
        or re.fullmatch(r"[0-9]{10}", cik) is None
        or int(cik) == 0
    ):
        raise ValueError("cik must be a positive zero-padded ten-digit CIK")
    if not isinstance(form, str) or _SUPPORTED_SEC_FORM.fullmatch(form) is None:
        raise ValueError("form must be a supported SEC financial form")
    if not isinstance(filename, str):
        raise ValueError("filename must be the filing archive filename")
    archive = _ARCHIVE_FILE.fullmatch(filename)
    if (
        archive is None
        or archive.group("accession") != accession
        or int(archive.group("cik")) != int(cik)
    ):
        raise ValueError("filename must match the accession and CIK")
    if (
        isinstance(observation_id, bool)
        or not isinstance(observation_id, int)
        or observation_id <= 0
    ):
        raise ValueError("observation_id must be a positive integer")
    return accession, cik, form, filename, observation_id


def _validate_parent_filing(
    parent,
    *,
    accession: str,
    cik: str,
    form: str,
    filename: str,
    observation_id: int,
) -> None:
    item = parent.item
    observation = parent.observation
    metadata = observation.canonical_metadata
    if observation.source_observation_id != observation_id:
        raise ValueError("cited SEC filing observation identity changed")
    if observation.state != "present":
        raise ValueError("cited SEC filing observation is not present")
    if (
        item.source_system != "SEC"
        or item.item_kind != "filing"
        or item.source_key != accession
        or item.issuer_source_identifier != cik
        or metadata.get("accession") != accession
        or metadata.get("cik") != cik
        or metadata.get("form") != form
        or metadata.get("archive_filename") != filename
    ):
        raise ValueError("cited SEC filing observation does not match the job")


def _root_resources(
    *,
    accession: str,
    cik: str,
    filename: str,
) -> tuple[_SecResource, ...]:
    compact_accession = accession.replace("-", "")
    archive_cik = str(int(cik))
    return (
        _SecResource(
            "complete_submission",
            "resource",
            f"{accession}/complete-submission",
            f"https://www.sec.gov/Archives/{filename}",
            True,
        ),
        _SecResource(
            "accession_inventory",
            "resource",
            f"{accession}/index",
            _ACCESSION_INDEX_URL.format(
                cik=archive_cik,
                accession=compact_accession,
            ),
            True,
            _validate_accession_inventory,
        ),
        _SecResource(
            "submissions",
            "aggregate",
            f"submissions/{cik}",
            _SUBMISSIONS_URL.format(cik=cik),
            False,
            _validate_submissions,
        ),
        _SecResource(
            "company_facts",
            "aggregate",
            f"companyfacts/{cik}",
            _COMPANY_FACTS_URL.format(cik=cik),
            False,
            _validate_company_facts,
        ),
    )


def _resource_identity(resource: _SecResource, *, cik: str, parent):
    return SourceItemIdentity(
        "SEC",
        resource.item_kind,
        resource.source_key,
        issuer_source_identifier=cik,
        parent_source_item_id=(
            parent.item.source_item_id if resource.parented else None
        ),
    )


def _resource_metadata(
    *,
    resource: _SecResource,
    role: str,
    accession: str,
    cik: str,
    form: str,
    filename: str,
    parent,
) -> dict[str, Any]:
    metadata = {
        "role": role,
        "cik": cik,
    }
    if resource.parented:
        metadata.update(
            {
                "accession": accession,
                "form": form,
                "archive_filename": filename,
                "parent_source_item_id": parent.item.source_item_id,
                "parent_observation_id": (
                    parent.observation.source_observation_id
                ),
                "parent_observation_sha256": (
                    parent.observation.observation_sha256
                ),
            }
        )
    return metadata


def _resource_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    accession: str,
    cik: str,
    observation_id: int,
) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "accession",
        "cik",
        "filing_observation_id",
        "roots",
    }:
        raise ValueError("SEC resource fetch checkpoint has an invalid shape")
    if (
        checkpoint["accession"] != accession
        or checkpoint["cik"] != cik
        or checkpoint["filing_observation_id"] != observation_id
    ):
        raise ValueError("SEC resource fetch checkpoint does not match its filing")
    roles = {
        "complete_submission",
        "accession_inventory",
        "submissions",
        "company_facts",
    }
    roots = checkpoint["roots"]
    if (
        not isinstance(roots, Mapping)
        or set(roots) != roles
        or any(
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in roots.values()
        )
    ):
        raise ValueError("SEC resource fetch checkpoint has invalid roots")
    return dict(checkpoint)


def _strict_json_object(data: bytes, label: str) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise ValueError(f"{label} must be exact response bytes")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate object keys")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"{label} contains {value}")

    try:
        value = json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_accession_inventory(
    data: bytes,
    accession: str,
    cik: str,
    _form: str,
    _filing_date: str,
) -> None:
    payload = _strict_json_object(data, "SEC accession inventory")
    directory = payload.get("directory")
    if not isinstance(directory, dict):
        raise ValueError("SEC accession inventory has no directory object")
    expected = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    name = directory.get("name")
    if not isinstance(name, str) or name.rstrip("/") != expected:
        raise ValueError("SEC accession inventory identifies another accession")
    items = directory.get("item")
    if not isinstance(items, list) or not items:
        raise ValueError("SEC accession inventory has no resource items")
    names = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("SEC accession inventory has a malformed resource item")
        item_name = item["name"]
        if not item_name or "/" in item_name or "\\" in item_name:
            raise ValueError("SEC accession inventory has a malformed resource name")
        names.append(item_name)
    if len(names) != len(set(names)):
        raise ValueError("SEC accession inventory has duplicate resource names")


def _validate_submissions(
    data: bytes,
    accession: str,
    cik: str,
    form: str,
    filing_date: str,
) -> None:
    payload = _strict_json_object(data, "SEC submissions")
    if not _same_cik(payload.get("cik"), cik):
        raise ValueError("SEC submissions identifies another issuer")
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions has no recent filings object")
    accessions = recent.get("accessionNumber")
    forms = recent.get("form")
    if (
        not isinstance(accessions, list)
        or not isinstance(forms, list)
        or len(accessions) != len(forms)
        or any(not isinstance(value, str) for value in accessions + forms)
    ):
        raise ValueError("SEC submissions recent filing arrays are malformed")
    matches = [
        index for index, value in enumerate(accessions) if value == accession
    ]
    if not matches:
        if _historical_submissions_file(payload, cik, filing_date) is None:
            raise SecResourcePending(
                "SEC submissions does not yet identify the filing accession"
            )
        return
    if len(matches) != 1 or forms[matches[0]] != form:
        raise ValueError("SEC submissions filing identity is ambiguous or mismatched")


def _validate_company_facts(
    data: bytes,
    accession: str,
    cik: str,
    form: str,
    _filing_date: str,
) -> None:
    payload = _strict_json_object(data, "SEC Company Facts")
    if not _same_cik(payload.get("cik"), cik):
        raise ValueError("SEC Company Facts identifies another issuer")
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("SEC Company Facts has no facts object")
    matches = []
    for taxonomy, concepts in facts.items():
        if not isinstance(taxonomy, str) or not taxonomy or not isinstance(concepts, dict):
            raise ValueError("SEC Company Facts has a malformed taxonomy")
        for concept, definition in concepts.items():
            if not isinstance(concept, str) or not concept or not isinstance(definition, dict):
                raise ValueError("SEC Company Facts has a malformed concept")
            units = definition.get("units")
            if not isinstance(units, dict):
                raise ValueError("SEC Company Facts concept has no units object")
            for unit, entries in units.items():
                if not isinstance(unit, str) or not unit or not isinstance(entries, list):
                    raise ValueError("SEC Company Facts has malformed units")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("SEC Company Facts has a malformed fact")
                    accn = entry.get("accn")
                    entry_form = entry.get("form")
                    if not isinstance(accn, str):
                        raise ValueError("SEC Company Facts has a malformed accession")
                    if not isinstance(entry_form, str):
                        raise ValueError("SEC Company Facts has a malformed form")
                    if accn == accession:
                        matches.append(entry_form)
    if not matches:
        raise SecResourcePending(
            "SEC Company Facts does not yet identify the filing accession"
        )
    if any(entry_form != form for entry_form in matches):
        raise ValueError("SEC Company Facts filing form does not match the job")


def _same_cik(value: Any, cik: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0 and value == int(cik)
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[0-9]+", value)
        and int(value) == int(cik)
    )


def _historical_submissions_file(
    payload: Mapping[str, Any], cik: str, filing_date: str
) -> str | None:
    try:
        filed = date.fromisoformat(filing_date)
    except (TypeError, ValueError):
        raise ValueError("SEC filing parent has an invalid filing date")
    filings = payload.get("filings")
    files = filings.get("files") if isinstance(filings, dict) else None
    if not isinstance(files, list):
        raise ValueError("SEC submissions has no historical files array")
    pattern = re.compile(rf"CIK{re.escape(cik)}-submissions-[0-9]{{3}}\.json")
    matched = None
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("SEC submissions has a malformed historical file")
        name = entry.get("name")
        filing_count = entry.get("filingCount")
        try:
            start = date.fromisoformat(entry.get("filingFrom"))
            end = date.fromisoformat(entry.get("filingTo"))
        except (TypeError, ValueError):
            raise ValueError("SEC submissions has a malformed historical date")
        if not isinstance(name, str) or pattern.fullmatch(name) is None:
            raise ValueError("SEC submissions has a malformed historical filename")
        if (
            isinstance(filing_count, bool)
            or not isinstance(filing_count, int)
            or filing_count <= 0
        ):
            raise ValueError("SEC submissions has a malformed historical count")
        if end < start:
            raise ValueError("SEC submissions has a reversed historical range")
        if start <= filed <= end:
            if matched is not None:
                raise ValueError("SEC submissions historical ranges are ambiguous")
            matched = name
    return matched


def _date_range(parameters: Mapping[str, Any]) -> tuple[date, date]:
    if not isinstance(parameters, Mapping):
        raise ValueError("SEC discovery parameters must be an object")
    if set(parameters) != {"start_date", "end_date"}:
        raise ValueError("SEC discovery requires only start_date and end_date")
    start = _iso_date("start_date", parameters["start_date"])
    end = _iso_date("end_date", parameters["end_date"])
    if end < start:
        raise ValueError("end_date must not be before start_date")
    if (end - start).days >= _MAX_DAYS:
        raise ValueError("SEC discovery range must contain at most 31 dates")
    return start, end


def _year_quarter(parameters: Mapping[str, Any]) -> tuple[int, int]:
    if not isinstance(parameters, Mapping) or set(parameters) != {"year", "quarter"}:
        raise ValueError("SEC reconciliation requires only year and quarter")
    year = parameters["year"]
    quarter = parameters["quarter"]
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
        raise ValueError("year must be a four-digit integer")
    if isinstance(quarter, bool) or not isinstance(quarter, int) or quarter not in range(1, 5):
        raise ValueError("quarter must be an integer from 1 through 4")
    return year, quarter


def _quarter_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    year: int,
    quarter: int,
) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "year",
        "quarter",
        "inventory_observation_id",
        "inventory_observation_sha256",
    }:
        raise ValueError("SEC reconciliation checkpoint has an invalid shape")
    if checkpoint["year"] != year or checkpoint["quarter"] != quarter:
        raise ValueError("SEC reconciliation checkpoint does not match its quarter")
    observation_id = checkpoint["inventory_observation_id"]
    digest = checkpoint["inventory_observation_sha256"]
    if (
        isinstance(observation_id, bool)
        or not isinstance(observation_id, int)
        or observation_id <= 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("SEC reconciliation checkpoint has invalid inventory identity")
    return dict(checkpoint)


def _filings_by_accession(
    filings: tuple[SecIndexFiling, ...],
) -> dict[str, SecIndexFiling]:
    indexed: dict[str, SecIndexFiling] = {}
    for filing in filings:
        if filing.accession in indexed:
            raise ValueError("SEC form index contains a duplicate accession")
        indexed[filing.accession] = filing
    return indexed


def _filing_comparison(filing: SecIndexFiling, quarter: str) -> dict[str, Any]:
    return {
        "form": filing.form,
        "company_name": filing.company_name,
        "cik": filing.cik,
        "filing_date": filing.filing_date.isoformat(),
        "archive_filename": filing.archive_filename,
        "accession": filing.accession,
        "quarter": quarter,
    }


def _stored_comparison(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {field: metadata.get(field) for field in _FILING_COMPARISON_FIELDS}


def _quarterly_filing_outcome_key(accession: str, inventory_sha256: str) -> str:
    return f"quarterly-filing:{accession}:{inventory_sha256}"


def _checked_outcome(*, action: str, filing, inventory) -> dict[str, Any]:
    return {
        "action": action,
        "source_item_id": filing.source_item_id,
        "source_observation_id": filing.source_observation_id,
        "observation_sha256": filing.observation_sha256,
        "inventory_source_item_id": inventory.item.source_item_id,
        "inventory_observation_id": inventory.observation.source_observation_id,
        "inventory_observation_sha256": (
            inventory.observation.observation_sha256
        ),
        "inventory_artifact_sha256": inventory.observation.artifact_sha256,
    }


def _iso_date(name: str, value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")
    return parsed


def _checkpoint_date(
    checkpoint: Mapping[str, Any] | None,
    start: date,
    end: date,
) -> date | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping):
        raise ValueError("SEC discovery checkpoint must be an object")
    if checkpoint.get("start_date") != start.isoformat() or checkpoint.get(
        "end_date"
    ) != end.isoformat():
        raise ValueError("SEC discovery checkpoint does not match its date range")
    through = _iso_date("checkpoint through_date", checkpoint.get("through_date"))
    if through < start or through > end:
        raise ValueError("SEC discovery checkpoint is outside its date range")
    return through
