from __future__ import annotations

import hashlib
import json
import re
from math import isfinite
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import normalize, store, sync
from .artifacts import ArtifactRepository, read_registered_artifact, store_evidence
from .company_import import _canonical_json
from .durable_jobs import (DurableJobRepository, DurableJobRecord, JobDeferred, LeaseLost, _SOURCE_RECEIPT_PREFIX,
    _bounded_schedule_parameters, _bounded_schedule_text, _schedule_datetime)
from .evidence import EvidenceBundle
from .figure_history import FigureHistoryRepository
from .models import CapturedSource
from . import exact_annual, primary_input_plan, primary_input_transport
from .job_runtime import JobContext, ScheduledOccurrence
from .normalize import FINANCIAL_FORMS
from .object_store import ImmutableObjectStore
from .shared_companies import PairReferenceCapacity, SharedCompanyRepository, SnapshotArtifact
from .source_observations import (SourceAcquisitionReceipt, SourceItemIdentity, SourceObservationRepository,
    _canonical_url, _optional_text, _receipt_text, observation_sha256)
from .sources import cover, inline_xbrl, sec_submission
from .sources import sec_json
from .sources.edgar import EdgarClient, EdgarError, NoXbrlDataError


_FORM_INDEX_HEADER = (
    "Form Type   Company Name                                                  "
    "CIK         Date Filed  File Name"
)
_FOLDED_FORM_INDEX_HEADER = (
    "Form Type   Company Name                                                  CIK",
    "      Date Filed  File Name",
)
_FOLDED_FORM_INDEX_SEPARATOR = "-" * 141
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
_RESOURCE_KINDS = frozenset({"sec-resource-fetch", "sec-resource-fetch-v2", "sec-resource-fetch-v3"})
_CAPTURE_REFERENCE_BYTES = 1024 * 1024
_CAPTURE_BODY_BYTES = 8 * 1024 * 1024
_CAPTURE_FACTS_BYTES = 32 * 1024 * 1024
_CAPTURE_TOTAL_BYTES = 64 * 1024 * 1024
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


@dataclass(frozen=True)
class SecIndexRow:
    physical_line: int
    entry_ordinal: int
    filing: SecIndexFiling


@dataclass(frozen=True)
class SecDailyAcquisition:
    stored: Any
    data: bytes | None
    artifact_sha256: str | None
    source_url: str
    etag: str | None
    last_modified: str | None
    media_type: str | None


def validate_finite_daily_source(stored, day, receipt_key):
    item, obs = stored.item, stored.observation
    quarter = (day.month - 1) // 3 + 1
    expected = dict(index_date=day.isoformat(), quarter=f"{day.year}Q{quarter}",
        http_media_type=obs.canonical_metadata.get("http_media_type"), http_headers_captured=obs.state == "present")
    if obs.state == "unavailable":
        expected["reason"] = "not_found"
    if (receipt_key != f"sec-finite:day:{day.isoformat()}" or item.source_system != "SEC"
            or item.item_kind != "inventory" or item.source_key != f"daily-index/{day.isoformat()}"
            or item.issuer_source_identifier is not None or item.parent_source_item_id is not None
            or obs.state not in {"present", "unavailable"} or obs.canonical_metadata != expected
            or stored.delivery is None or stored.delivery.item_key != f"daily-index:{day.isoformat()}"):
        raise ValueError("finite daily acquisition differs from its declared date/receipt")


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


def parse_all_form_index(data: bytes, *, maximum_rows: int | None = None, maximum_row_bytes: int | None = None) -> tuple[SecIndexRow, ...]:
    """Retain every validated nonblank row and its original physical position."""
    for name, value in (("maximum_rows", maximum_rows), ("maximum_row_bytes", maximum_row_bytes)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer")
    if not isinstance(data, bytes):
        raise ValueError("SEC form index must be bytes")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("SEC form index must be ASCII") from error

    header_rows = [index for index, line in enumerate(lines)
                   if line in (_FORM_INDEX_HEADER, _FOLDED_FORM_INDEX_HEADER[0])]
    if len(header_rows) != 1:
        raise ValueError("SEC form index header shape changed")
    header_row = header_rows[0]
    if lines[header_row] == _FORM_INDEX_HEADER:
        separator_row = header_row + 1
        valid_separator = separator_row < len(lines) and _SEPARATOR.fullmatch(lines[separator_row]) is not None
    else:
        if header_row + 1 >= len(lines) or lines[header_row + 1] != _FOLDED_FORM_INDEX_HEADER[1]:
            raise ValueError("SEC form index header shape changed")
        separator_row = header_row + 2
        valid_separator = separator_row < len(lines) and lines[separator_row] == _FOLDED_FORM_INDEX_SEPARATOR
    if not valid_separator:
        raise ValueError("SEC form index separator shape changed")
    data_start = separator_row + 1

    filings = []
    for physical_line, row in enumerate(lines[data_start:], start=data_start):
        if not row.strip():
            continue
        if maximum_rows is not None and len(filings) >= maximum_rows:
            raise ValueError("SEC index exceeds remaining row ceiling")
        if maximum_row_bytes is not None and len(row) > maximum_row_bytes:
            raise ValueError("SEC index row exceeds encoded record admission ceiling")
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
        filings.append(
            SecIndexRow(physical_line, len(filings), SecIndexFiling(
                form=form,
                company_name=company_name,
                cik=f"{int(cik_value):010d}",
                filing_date=filing_date,
                archive_filename=archive_filename,
                accession=match.group("accession"),
                source_row=row,
            ))
        )
    return tuple(filings)



def parse_form_index(data: bytes) -> tuple[SecIndexFiling, ...]:
    """Preserve the existing ordered financial-prefix view of the index."""
    return tuple(row.filing for row in parse_all_form_index(data)
                 if row.filing.form.startswith(_SEC_FINANCIAL_PREFIXES))


def _resource_child_key(kind, filing, observation, context, occurrence_bound):
    suffix = f"{filing.cik}:{filing.accession}:{observation.observation_sha256}"
    if not occurrence_bound:
        return f"{kind}:{suffix}"
    return resource_child_key_for_occurrence(kind, filing, observation, context.job.occurrence_id)


def resource_child_key_for_occurrence(kind, filing, observation, occurrence):
    suffix = f"{filing.cik}:{filing.accession}:{observation.observation_sha256}"
    if type(occurrence) is not int or occurrence <= 0:
        raise ValueError("SEC resource check requires a positive producer occurrence ID")
    return f"{kind}:occurrence:{occurrence}:{suffix}"


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
        resource_kind: str = "sec-resource-fetch",
        occurrence_bound: bool = False,
    ) -> None:
        if not isinstance(resource_kind, str) or resource_kind not in _RESOURCE_KINDS:
            raise ValueError("unsupported SEC resource kind")
        if resource_kind == "sec-resource-fetch-v3" and occurrence_bound is not True:
            raise ValueError("resource-v3 discovery requires occurrence-bound requests")
        if type(occurrence_bound) is not bool or (occurrence_bound and resource_kind not in {"sec-resource-fetch-v2", "sec-resource-fetch-v3"}):
            raise ValueError("occurrence-bound checks require the exact resource-v2 contract")
        self._resource_kind = resource_kind
        self._occurrence_bound = occurrence_bound
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
        acquired = self._acquire_daily_index(context, day)
        if acquired.data is None:
            return
        for filing in parse_form_index(acquired.data):
            stored = self._record_daily_filing(context, day, filing, acquired)
            self._enqueue_daily_resource(context, filing, stored.observation)

    def _acquire_daily_index(
        self, context: JobContext, day: date, *, request_identity_sha256=None,
        maximum_bytes=None, maximum_text_bytes=None,
    ) -> SecDailyAcquisition:
        quarter = (day.month - 1) // 3 + 1
        url = _DAILY_INDEX_URL.format(year=day.year, quarter=quarter, ymd=day.strftime("%Y%m%d"))
        metadata = {"index_date": day.isoformat(), "quarter": f"{day.year}Q{quarter}"}
        key = f"sec-finite:day:{day.isoformat()}"
        if request_identity_sha256 is not None:
            existing = self._observations.acquisition_receipt(producer_job_id=context.job.job_id,
                receipt_key=key, request_identity_sha256=request_identity_sha256)
            if existing is not None:
                item, obs = existing.item, existing.observation
                validate_finite_daily_source(existing, day, key)
                artifact, raw = (read_registered_artifact(obs.artifact_sha256, self._object_store,
                    self._artifacts, maximum=maximum_bytes, label="finite daily index")
                    if obs.state == "present" else (None, None))
                return SecDailyAcquisition(existing, raw, obs.artifact_sha256, obs.source_url,
                    obs.etag, obs.last_modified, obs.canonical_metadata["http_media_type"])
        values = dict(item=SourceItemIdentity("SEC", "inventory", f"daily-index/{day.isoformat()}"),
            item_outcome_key=f"daily-index:{day.isoformat()}", metadata=metadata,
            source_url=url)
        try:
            result = (self._edgar.fetch(url) if maximum_bytes is None
                else self._edgar.fetch(url, maximum_bytes=maximum_bytes))
        except NoXbrlDataError:
            values.update(state="unavailable", metadata={**metadata, "reason": "not_found"},
                detected_at=self._clock())
            if request_identity_sha256 is not None:
                values["metadata"].update(http_media_type=None, http_headers_captured=False)
            stored = self._record_daily_source(context, request_identity_sha256, key, values)
            if request_identity_sha256 is not None:
                validate_finite_daily_source(stored, day, key)
            return SecDailyAcquisition(stored, None, None, url, None, None, None)
        if maximum_text_bytes is not None:
            _finite_transport_texts(result, maximum_text_bytes)
        artifact = store_evidence(result.data, result.media_type or "application/octet-stream",
            self._object_store, self._artifacts)
        if request_identity_sha256 is not None:
            values["metadata"].update(http_media_type=result.media_type, http_headers_captured=True)
        values.update(state="present", source_url=result.url, artifact_sha256=artifact.content_sha256,
            etag=result.etag, last_modified=result.last_modified, detected_at=self._clock())
        stored = self._record_daily_source(context, request_identity_sha256, key, values)
        if request_identity_sha256 is not None:
            obs = stored.observation
            validate_finite_daily_source(stored, day, key)
            return SecDailyAcquisition(stored, result.data, artifact.content_sha256, obs.source_url,
                obs.etag, obs.last_modified, obs.canonical_metadata["http_media_type"])
        return SecDailyAcquisition(stored, result.data, artifact.content_sha256, result.url,
            result.etag, result.last_modified, result.media_type)

    def _record_daily_source(self, context, request_identity_sha256, receipt_key, values):
        if request_identity_sha256 is None:
            return self._observations.record(context.lease, **values)
        return self._observations.record_with_acquisition_receipt(context.lease,
            receipt_key=receipt_key, request_identity_sha256=request_identity_sha256, **values)

    def _record_daily_filing(
        self, context, day, filing, acquired, *, request_identity_sha256=None, receipt_key=None,
    ):
        quarter = (day.month - 1) // 3 + 1
        metadata = dict(form=filing.form, company_name=filing.company_name, cik=filing.cik,
            filing_date=filing.filing_date.isoformat(), archive_filename=filing.archive_filename,
            accession=filing.accession, source_row=filing.source_row, index_date=day.isoformat(),
            quarter=f"{day.year}Q{quarter}")
        if request_identity_sha256 is not None:
            metadata.update(http_media_type=acquired.media_type, http_headers_captured=True)
            existing = self._observations.acquisition_receipt(producer_job_id=context.job.job_id,
                receipt_key=receipt_key, request_identity_sha256=request_identity_sha256)
            if existing is not None:
                item, obs = existing.item, existing.observation
                if (item.source_system != "SEC" or item.item_kind != "filing"
                        or item.source_key != filing.accession or item.issuer_source_identifier != filing.cik
                        or item.parent_source_item_id is not None or obs.state != "present"
                        or obs.canonical_metadata != metadata or obs.source_url != acquired.source_url
                        or obs.artifact_sha256 != acquired.artifact_sha256
                        or obs.etag != acquired.etag or obs.last_modified != acquired.last_modified):
                    raise ValueError("finite filing acquisition differs from retained index row")
                return existing
        values = dict(item=SourceItemIdentity("SEC", "filing", filing.accession, filing.cik),
            item_outcome_key=f"filing:{filing.accession}:{acquired.stored.observation.observation_sha256}",
            state="present", metadata=metadata, source_url=acquired.source_url,
            artifact_sha256=acquired.artifact_sha256, etag=acquired.etag,
            last_modified=acquired.last_modified, detected_at=self._clock())
        return self._record_daily_source(context, request_identity_sha256, receipt_key, values)

    def _enqueue_daily_resource(self, context, filing, observation):
        immediate = datetime.combine(filing.filing_date, time.min, tzinfo=timezone.utc)
        key = _resource_child_key(self._resource_kind, filing, observation, context, self._occurrence_bound)
        job = self._jobs.enqueue_child(context.lease, child_key=key, scheduled_for=immediate,
            kind=self._resource_kind, parameters=dict(accession=filing.accession, cik=filing.cik,
                form=filing.form, filename=filing.archive_filename,
                observation_id=observation.source_observation_id), due_at=immediate,
            priority=context.job.priority, now=self._clock)
        return key, job

    def retained_day(self, context, day, *, request_identity_sha256, maximum_bytes,
                     maximum_text_bytes, maximum_rows, maximum_row_bytes):
        if self._resource_kind not in {"sec-resource-fetch-v2", "sec-resource-fetch-v3"} or not self._occurrence_bound:
            raise ValueError("finite requests require occurrence-bound resource-v2")
        acquired = self._acquire_daily_index(context, day,
            request_identity_sha256=request_identity_sha256, maximum_bytes=maximum_bytes,
            maximum_text_bytes=maximum_text_bytes)
        rows = (parse_all_form_index(acquired.data, maximum_rows=maximum_rows,
            maximum_row_bytes=maximum_row_bytes) if acquired.data is not None else ())
        return acquired, rows

    def retained_day_requests(self, context, day, acquired, rows, *, request_identity_sha256):
        """Caller has admitted the complete day frontier before starting fanout."""
        for row in rows:
            if context.stopping.is_set():
                raise SecDiscoveryStopped("finite discovery stopped before the next row")
            route, _ = resource_route(row.filing)
            if route != "RESOURCE_V2":
                yield row, None, None, None
                continue
            key = f"sec-finite:filing:{day.isoformat()}:{row.entry_ordinal}"
            stored = self._record_daily_filing(context, day, row.filing, acquired,
                request_identity_sha256=request_identity_sha256, receipt_key=key)
            child_key, child = self._enqueue_daily_resource(context, row.filing, stored.observation)
            yield row, stored, child_key, child


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
        resource_kind: str = "sec-resource-fetch",
        occurrence_bound: bool = False,
    ) -> None:
        if not isinstance(resource_kind, str) or resource_kind not in {"sec-resource-fetch", "sec-resource-fetch-v2"}:
            raise ValueError("unsupported SEC resource kind")
        if type(occurrence_bound) is not bool or (occurrence_bound and resource_kind != "sec-resource-fetch-v2"):
            raise ValueError("occurrence-bound checks require the exact resource-v2 contract")
        self._resource_kind = resource_kind
        self._occurrence_bound = occurrence_bound
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
                if self._occurrence_bound or prior.detecting_job_id == context.job.job_id:
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
            child_key=_resource_child_key(
                self._resource_kind, filing, observation, context, self._occurrence_bound
            ),
            scheduled_for=immediate,
            kind=self._resource_kind,
            parameters={
                "accession": filing.accession,
                "cik": filing.cik,
                "form": filing.form,
                "filename": filing.archive_filename,
                "observation_id": observation.source_observation_id,
            },
            due_at=immediate,
            priority=context.job.priority,
            now=self._clock,
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
        jobs: DurableJobRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        annotate_annual: bool = False,
        retain_partial_acquisitions: bool = False,
    ) -> None:
        if type(retain_partial_acquisitions) is not bool:
            raise ValueError("retain_partial_acquisitions must be an exact boolean")
        if retain_partial_acquisitions and annotate_annual is not True:
            raise ValueError("partial acquisition retention requires annotated capture2")
        self._edgar = edgar
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._jobs = jobs
        self._clock = clock
        self._annotate_annual = annotate_annual
        self._retain_partial_acquisitions = retain_partial_acquisitions

    def recover_completed_capture(self, *, job_id, occurrence_id, schedule_key, scheduled_for):
        """Authenticate one persisted capture without fetching, enqueueing or deriving."""
        _capture_id("job_id", job_id)
        _capture_id("occurrence_id", occurrence_id)
        _bounded_schedule_text("schedule_key", schedule_key, 1024)
        scheduled = _schedule_datetime("scheduled_for", scheduled_for)
        identity = dict(job_id=job_id, occurrence_id=occurrence_id,
                        schedule_key=schedule_key, scheduled_for=scheduled)
        job = self._jobs.scheduled_job(**identity)
        if job.job_id != job_id or job.occurrence_id != occurrence_id:
            raise ValueError("persisted resource IDs differ from requested relation")
        parameters, _ = _bounded_schedule_parameters(job.parameters)
        checkpoint = None if job.checkpoint is None else _bounded_schedule_parameters(job.checkpoint)[0]
        if job.kind not in _RESOURCE_KINDS:
            raise ValueError("completed capture requires a SEC resource job")
        accession, cik, form, filename, observation_id = _resource_parameters(parameters)
        _capture_id("filing observation", observation_id)
        stamp = _capture_job_stamp(job)
        job_reference = {name: value for name, value in stamp.items()
                         if name not in {"parameters_json", "checkpoint_json"}}
        job_reference.update(schedule_key=schedule_key, scheduled_for=scheduled.isoformat(), parameters=parameters)
        result = dict(operation_revision=1, status="NOT_COMPLETED", job=job_reference,
                      checkpoint=checkpoint, roots=[], gap=None,
                      bytes_verified=False, all_applicable_root_structures_validated=False,
                      financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED")

        def finish(value):
            current = self._jobs.scheduled_job(**identity)
            if _capture_job_stamp(current) != stamp:
                raise ValueError("persisted resource result changed during capture recovery")
            detached, _, exceeded = _capture_result_json(value)
            if exceeded:
                raise ValueError("compact capture result exceeds its reference ceiling")
            return detached

        def capacity(phase, required, limit, *, role=None, lower_bound=False):
            return finish(dict(operation_revision=1, status="CAPACITY_UNAVAILABLE",
                job=dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=schedule_key,
                         scheduled_for=scheduled.isoformat(), kind=job.kind, status=job.status),
                roots=[], gap=dict(phase=phase, role=role, required_bytes=required,
                                   limit_bytes=limit, required_is_lower_bound=lower_bound),
                bytes_verified=False, all_applicable_root_structures_validated=False,
                financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED"))

        if job.status in {"pending", "running", "retry_wait", "failed"}:
            result["status"] = "FAILED" if job.status == "failed" else "NOT_COMPLETED"
            _, size, exceeded = _capture_result_json(result)
            return capacity("result_references", size, _CAPTURE_REFERENCE_BYTES, lower_bound=True) if exceeded else finish(result)
        if job.status != "succeeded":
            raise ValueError("invalid persisted resource status")
        captured = _resource_checkpoint(checkpoint, accession=accession, cik=cik,
                                        observation_id=observation_id)
        if captured is None:
            raise ValueError("succeeded resource has no completed checkpoint")
        _capture_id("checkpoint filing observation", captured["filing_observation_id"])
        if captured.get("capture_revision") != 2:
            if job.kind in {"sec-resource-fetch-v2", "sec-resource-fetch-v3"}:
                raise ValueError("resource-v2 completed capture requires revision2")
            result.update(status="ASSOCIATION_UNAVAILABLE", gap=dict(reason="ORIGINAL_DELIVERIES_UNRECORDED"))
            _, size, exceeded = _capture_result_json(result)
            return capacity("result_references", size, _CAPTURE_REFERENCE_BYTES, lower_bound=True) if exceeded else finish(result)
        parent = self._observations.sec_filing(observation_id)
        _validate_parent_filing(parent, accession=accession, cik=cik, form=form,
                               filename=filename, observation_id=observation_id)
        _capture_id("parent item", parent.item.source_item_id)
        _capture_id("parent observation", parent.observation.source_observation_id)
        parent_metadata, _ = _bounded_schedule_parameters(parent.observation.canonical_metadata)
        _capture_sha(parent.observation.observation_sha256)
        result["filing"] = dict(source_item_id=parent.item.source_item_id,
            source_observation_id=parent.observation.source_observation_id,
            observation_sha256=parent.observation.observation_sha256, metadata=parent_metadata,
            index_artifact_verified=False)
        resources = _root_resources(accession=accession, cik=cik, filename=filename)
        prepared, unique, gaps, missing_linkage = [], {}, [], False
        for resource in resources:
            role = resource.role
            stored = _retained_root_source(self._observations, captured["roots"][role],
                                           role=role, parent=parent, accession=accession, cik=cik)
            item, observation = stored.item, stored.observation
            metadata, _ = _bounded_schedule_parameters(observation.canonical_metadata)
            _capture_id("source item", item.source_item_id)
            _capture_id("source observation", observation.source_observation_id)
            _capture_id("observation source item", observation.source_item_id)
            if observation.source_item_id != item.source_item_id:
                raise ValueError("root observation belongs to another source item")
            for value in (item.source_system, item.item_kind, item.source_key, item.issuer_source_identifier):
                _capture_text(value)
            if item.parent_source_item_id is not None:
                _capture_id("root parent item", item.parent_source_item_id)
            _capture_sha(observation.observation_sha256)
            if observation.observation_sha256 != captured["roots"][role]:
                raise ValueError("root source digest differs from checkpoint")
            expected_item = _resource_identity(resource, cik=cik, parent=parent)
            if (item.item_kind != expected_item.item_kind or item.source_key != expected_item.source_key
                    or item.parent_source_item_id != expected_item.parent_source_item_id):
                raise ValueError("captured root item identity differs")
            expected_metadata = _resource_metadata(resource=resource, role=role, accession=accession,
                cik=cik, form=form, filename=filename, parent=parent)
            for name, value in expected_metadata.items():
                if name not in metadata and name in {"parent_source_item_id", "parent_observation_id", "parent_observation_sha256"}:
                    missing_linkage = True
                elif name not in metadata or _canonical_json(metadata[name]) != _canonical_json(value):
                    raise ValueError("captured root original filing context differs")
            supplied = captured["root_deliveries"][role]
            delivery = _retained_root_delivery(self._observations, stored, supplied,
                                               compare_instants=True, strict_revision=True)
            if delivery.job_id != job.job_id:
                raise ValueError("captured root delivery belongs to another producer job")
            _capture_sha(observation.artifact_sha256)
            artifact = self._artifacts.get(observation.artifact_sha256)
            if (artifact.content_sha256 != observation.artifact_sha256
                    or artifact.object_key != f"raw/sha256/{observation.artifact_sha256}"
                    or type(artifact.byte_size) is not int or artifact.byte_size < 0):
                raise ValueError("invalid registered artifact metadata")
            limit = _CAPTURE_FACTS_BYTES if role == "company_facts" else _CAPTURE_BODY_BYTES
            if artifact.byte_size > limit:
                gaps.append((role, artifact.byte_size, limit))
            unique[artifact.content_sha256] = artifact.byte_size
            reference = dict(role=role, source_item_id=item.source_item_id,
                source_observation_id=observation.source_observation_id,
                observation_sha256=observation.observation_sha256, metadata=metadata,
                source_url=_capture_text(observation.source_url), etag=_capture_text(observation.etag),
                last_modified=_capture_text(observation.last_modified), delivery=dict(supplied),
                artifact=dict(content_sha256=artifact.content_sha256, object_key=artifact.object_key,
                              byte_size=artifact.byte_size, registration_media_type=_capture_text(artifact.media_type)),
                capture_fidelity="RAW_HTTP_BYTES", original_http_content_type_captured=False,
                original_http_content_type=None, bytes_verified=False, root_structure_validated=False)
            result["roots"].append(reference)
            prepared.append((resource, artifact, reference, limit))
        if gaps:
            role, required, limit = gaps[0]
            return capacity("body", required, limit, role=role)
        if sum(unique.values()) > _CAPTURE_TOTAL_BYTES:
            return capacity("unique_bodies", sum(unique.values()), _CAPTURE_TOTAL_BYTES)
        _, size, exceeded = _capture_result_json(result)
        if exceeded:
            return capacity("result_references", size, _CAPTURE_REFERENCE_BYTES, lower_bound=True)
        if missing_linkage:
            result.update(status="ASSOCIATION_UNAVAILABLE", gap=dict(reason="ORIGINAL_PARENT_REVISION_UNRECORDED"))
            _, size, exceeded = _capture_result_json(result)
            return capacity("result_references", size, _CAPTURE_REFERENCE_BYTES, lower_bound=True) if exceeded else finish(result)
        for resource, artifact, reference, limit in prepared:
            registered, payload = read_registered_artifact(artifact.content_sha256,
                self._object_store, self._artifacts, maximum=limit)
            if registered != artifact:
                raise ValueError("registered root metadata changed during recovery")
            reference["bytes_verified"] = True
            if resource.validator is not None:
                try:
                    resource.validator(payload, accession, cik, form, parent_metadata.get("filing_date", ""))
                except SecResourcePending as error:
                    if type(error) is not SecResourcePending:
                        raise
                    reason = _capture_text(str(error))
                    result = dict(operation_revision=1, status="ROOT_STRUCTURE_UNAVAILABLE",
                        job=dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=schedule_key,
                                 scheduled_for=scheduled.isoformat(), kind=job.kind, status=job.status),
                        roots=[{name: ref[name] for name in ("role", "source_item_id", "source_observation_id",
                            "observation_sha256", "bytes_verified", "root_structure_validated")} for ref in result["roots"]],
                        gap=dict(role=resource.role, reason=reason), bytes_verified=False,
                        all_applicable_root_structures_validated=False, financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED")
                    del payload
                    return finish(result)
                reference["root_structure_validated"] = True
            del payload
        result.update(status="VERIFIED", bytes_verified=True, all_applicable_root_structures_validated=True)
        return finish(result)

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        if context.job.kind == "sec-resource-fetch-v3" and (self._annotate_annual is not True
                or self._retain_partial_acquisitions is not True):
            raise ValueError("resource-v3 requires its explicit partial capture2 handler")
        partial_job = _partial_resource_job(context) if self._retain_partial_acquisitions else None
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
            if self._annotate_annual and completed.get("capture_revision") != 2:
                raise ValueError("legacy completed capture has no exact delivery contract")
            self._enqueue_candidate(context, completed)
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
        deliveries = {}
        filing_date = parent.observation.canonical_metadata.get("filing_date", "")
        partial_request = (_partial_resource_request(partial_job, parent, resources, cik, filing_date)
                           if partial_job is not None else None)
        partial_budget = dict(bytes=0, artifacts={})
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
            if partial_request is not None:
                stored, encoded_delivery = self._acquire_partial_root(context, resource, parent,
                    metadata, partial_request, partial_budget, accession, cik, form, filing_date)
                roots[resource.role] = stored.observation.observation_sha256
                deliveries[resource.role] = encoded_delivery
                continue
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
                metadata=({**metadata, "capture_revision": 2, "capture_fidelity": "RAW_HTTP_BYTES"}
                          if self._annotate_annual else metadata),
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
            if self._annotate_annual:
                if stored.delivery is None:
                    raise ValueError("annotated capture requires its actual recorded delivery")
                deliveries[resource.role] = _delivery_values(stored.delivery)

        checkpoint = {
            "accession": accession,
            "cik": cik,
            "filing_observation_id": observation_id,
            "roots": roots,
        }
        if self._annotate_annual:
            checkpoint.update(capture_revision=2, root_deliveries=deliveries)
        if partial_request is not None:
            _bounded_schedule_parameters(checkpoint)
        self._enqueue_candidate(context, checkpoint)
        context.save_checkpoint(checkpoint)
        return checkpoint

    def _acquire_partial_root(self, context, resource, parent, metadata, request, budget,
                              accession, cik, form, filing_date):
        ready_key = f"sec-resource-partial:1:ready:{resource.role}"
        failure_key = f"sec-resource-partial:1:failure:{resource.role}:{context.lease.generation}"
        identity = _resource_identity(resource, cik=cik, parent=parent)
        lookup = dict(producer_job_id=context.job.job_id, request_identity_sha256=request)
        ready = self._observations.acquisition_receipt(receipt_key=ready_key, **lookup)
        failed = self._observations.acquisition_receipt(receipt_key=failure_key, **lookup)
        if ready is not None and failed is not None:
            _partial_root_admission(failed, identity, metadata, resource, request, failure_key, context)
            if ready.delivery.ownership_generation == context.lease.generation:
                raise ValueError("partial acquisition has contradictory same-generation phases")
        stored = ready if ready is not None else failed
        key = ready_key if ready is not None else failure_key
        maximum = _partial_root_maximum(resource)
        if stored is not None:
            phase, delivery = _partial_root_admission(stored, identity, metadata, resource, request, key, context)
            if stored.observation.artifact_sha256 is not None:
                _, raw = read_registered_artifact(stored.observation.artifact_sha256,
                    self._object_store, self._artifacts, maximum=maximum, label="partial SEC root")
                _partial_root_bytes(raw, maximum, budget)
            else:
                raw = None
            if phase["phase"] == "FAILED_PENDING":
                _partial_root_pending(context, resource, stored, phase["failure"])
            if resource.validator is not None:
                resource.validator(raw, accession, cik, form, filing_date)
            return stored, delivery

        failure = None
        try:
            result = self._edgar.fetch(resource.url, maximum_bytes=maximum)
        except NoXbrlDataError as error:
            if type(error) is not NoXbrlDataError:
                raise
            result = None
            failure = dict(kind="NoXbrlDataError", message=str(error),
                           raised_message=f"SEC root resource is pending: {resource.role}")
        if result is not None:
            _partial_root_bytes(result.data, maximum, budget)
            for name in ("url", "media_type", "etag", "last_modified"):
                _capture_text(getattr(result, name))
        phase = dict(revision=1, phase="READY" if failure is None else "FAILED_PENDING",
            requested_url=resource.url,
            http_headers_captured=result is not None,
            http_response_url=None if result is None else result.url,
            http_media_type=None if result is None else result.media_type,
            http_etag=None if result is None else result.etag,
            http_last_modified=None if result is None else result.last_modified, failure=failure)
        _partial_phase(phase)
        if result is not None:
            if resource.validator is not None:
                try:
                    resource.validator(result.data, accession, cik, form, filing_date)
                except SecResourcePending as error:
                    if type(error) is not SecResourcePending:
                        raise
                    failure = dict(kind="SecResourcePending", message=str(error), raised_message=str(error))
        phase.update(phase="READY" if failure is None else "FAILED_PENDING", failure=failure)
        _partial_phase(phase)
        retained_metadata = {**metadata, "partial_acquisition": phase}
        if result is None:
            retained_metadata["reason"] = "not_found"
            artifact = None
        else:
            retained_metadata.update(capture_revision=2, capture_fidelity="RAW_HTTP_BYTES")
        _bounded_schedule_parameters(retained_metadata)
        if result is not None:
            artifact = store_evidence(result.data, result.media_type or "application/octet-stream",
                                      self._object_store, self._artifacts)
        key = ready_key if failure is None else failure_key
        stored = self._observations.record_with_acquisition_receipt(context.lease, receipt_key=key,
            request_identity_sha256=request, item=identity, item_outcome_key=f"root:{resource.role}",
            state="pending" if result is None else "present", metadata=retained_metadata,
            source_url=resource.url if result is None else result.url,
            artifact_sha256=None if artifact is None else artifact.content_sha256,
            etag=None if result is None else result.etag,
            last_modified=None if result is None else result.last_modified, detected_at=self._clock())
        phase, delivery = _partial_root_admission(stored, identity, metadata, resource, request, key, context)
        if failure is not None:
            _partial_root_pending(context, resource, stored, phase["failure"])
        return stored, delivery

    def derive_registered_auxiliaries(self, context: JobContext) -> Mapping[str, Any]:
        """Retain exact auxiliary bytes from a registered submission under this lease."""
        if context.job.kind != "sec-submission-parts-v1":
            raise ValueError("submission parts require their exact producer kind")
        admitted = _admit_submission_part_request(self, context)
        request = admitted.request_identity_sha256
        names = tuple(document for document, _ in admitted.auxiliaries)
        _submission_part_live_boundary(self, context, admitted.budget)
        container = _read_retained_body(self, admitted, admitted.container_artifact.content_sha256)
        admitted.budget.parser(len(container), 6 * len(container))
        decoded = sec_submission.decode_named_documents(container, names)
        if (decoded.container_sha256 != admitted.container_artifact.content_sha256
                or decoded.container_byte_size != admitted.container_artifact.byte_size
                or decoded.work_bytes > 6 * len(container)):
            raise ValueError("submission decoder differs from admitted container")
        prepared = []
        output_sizes = {digest: artifact.byte_size for digest, artifact in admitted.artifacts.items()}
        for part, (document, role) in zip(decoded.documents, admitted.auxiliaries, strict=True):
            if part.filename != document or document == admitted.primary_document:
                raise ValueError("submission auxiliary selection differs or includes primary")
            _submission_part_output_frontier(output_sizes, part)
            resource = _SecResource(role, "resource", admitted.accession + "/" + document,
                                    admitted.container.observation.source_url, True)
            item = _resource_identity(resource, cik=admitted.cik, parent=admitted.parent)
            metadata = _resource_metadata(resource=resource, role=role, accession=admitted.accession,
                cik=admitted.cik, form=admitted.form, filename=admitted.filename, parent=admitted.parent)
            metadata.update(document=document, derived_document=_submission_part_origin(admitted, part))
            metadata, metadata_bytes = _submission_part_validate_child(admitted, metadata, document, role,
                part.content_sha256, len(part.payload))
            _reserve_submission_part_record(admitted.budget, metadata_bytes)
            prepared.append((part, item, metadata, admitted.receipts.get(document)))

        outcomes = []
        for part, item, metadata, prior in prepared:
            if context.stopping.is_set():
                raise SecDiscoveryStopped("submission parts stopped before the next auxiliary")
            _recheck_submission_part_request(self, admitted, context)
            key = "sec-submission-part:1:" + part.filename
            if prior is not None:
                _admit_submission_part_replay(self, admitted, prior, item, metadata, part, context)
                stored = prior
            else:
                # Artifact and source commits are separate; failures leave their actual custody intact.
                _reserve_submission_part_store(admitted.budget, len(part.payload))
                _submission_part_live_boundary(self, context, admitted.budget)
                verified = self._object_store.put_verified(part.payload, "application/octet-stream")
                _recheck_submission_part_request(self, admitted, context)
                artifact = self._artifacts.add_verified(verified, "application/octet-stream")
                if (artifact.content_sha256 != part.content_sha256 or artifact.byte_size != len(part.payload)
                        or artifact.object_key != "raw/sha256/" + part.content_sha256):
                    raise ValueError("stored auxiliary differs from verified container bytes")
                _admit_submission_part_artifact(self, admitted, artifact, part)
                _recheck_submission_part_request(self, admitted, context)
                stored = self._observations.record_with_acquisition_receipt(context.lease,
                    receipt_key=key, request_identity_sha256=request, item=item,
                    item_outcome_key="derived:" + admitted.accession + ":" + part.filename,
                    state="present", metadata=metadata,
                    source_url=admitted.container.observation.source_url,
                    artifact_sha256=artifact.content_sha256, detected_at=self._clock(), _raw_work_budget=admitted.budget)
                _admit_submission_part_replay(self, admitted, stored, item, metadata, part, context)
            receipt = SourceAcquisitionReceipt(context.job.job_id, request, key, item,
                                               stored.observation.observation_sha256, stored.delivery)
            outcomes.append(receipt.to_outcome(_raw_work_budget=admitted.budget))
        _recheck_submission_part_request(self, admitted, context)
        result = dict(submission_part_revision=1, request_identity_sha256=request,
                      extraction_job=dict(admitted.producer_association), acquisitions=outcomes)
        detached, encoded_checkpoint = _submission_part_codec(result, admitted.budget,
                                               maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
        _reserve_submission_part_checkpoint(admitted.budget, len(encoded_checkpoint))
        _submission_part_live_boundary(self, context, admitted.budget)
        context.save_checkpoint(detached, _raw_work_budget=admitted.budget)
        _recheck_submission_part_request(self, admitted, context)
        return detached

    def enqueue_planned_candidate(self, context, checkpoint, *, primary_input_plan_sha256):
        """Explicit reviewed registered plan; the automatic unplanned path stays separate."""
        accession, cik, _, _, observation_id = _resource_parameters(context.job.parameters)
        checkpoint = _resource_checkpoint(checkpoint, accession=accession, cik=cik, observation_id=observation_id)
        if checkpoint is None or checkpoint.get("capture_revision") != 2:
            raise ValueError("planned SEC staging requires captured revision2")
        _, raw = read_registered_artifact(primary_input_plan_sha256, self._object_store, self._artifacts,
                                          maximum=primary_input_plan.MAXIMUM_BYTES)
        plan = primary_input_plan.decode_primary_plan(raw)
        if plan.cik != checkpoint["cik"]:
            raise ValueError("planned SEC artifact identifies another issuer")
        self._enqueue_candidate(context, checkpoint, primary_input_plan_sha256=primary_input_plan_sha256)

    def _enqueue_candidate(
        self, context: JobContext, checkpoint: Mapping[str, Any], *, primary_input_plan_sha256=None
    ):
        request = self._candidate_stage_request(context.job, checkpoint,
            primary_input_plan_sha256=primary_input_plan_sha256)
        return self._enqueue_stage_request(context, request)

    def candidate_stage_request(
        self, resource_job: DurableJobRecord, checkpoint: Mapping[str, Any], *, primary_input_plan_sha256=None, retained_stage=None,
    ) -> ScheduledOccurrence:
        """Bounded scheduling declaration only; source capture authentication is separate."""
        if not isinstance(resource_job, DurableJobRecord) or resource_job.kind not in _RESOURCE_KINDS:
            raise ValueError("candidate descriptor requires an actual resource job record/kind")
        if type(resource_job.job_id) is not int or not 0 < resource_job.job_id < 2**63:
            raise ValueError("resource job ID must be positive signed64bit")
        parameters, _ = _bounded_schedule_parameters(resource_job.parameters)
        copied, _ = _bounded_schedule_parameters(checkpoint)
        accession, cik, _, _, observation_id = _resource_parameters(parameters)
        captured = _resource_checkpoint(copied, accession=accession, cik=cik, observation_id=observation_id)
        if captured is None or resource_job.kind in {"sec-resource-fetch-v2", "sec-resource-fetch-v3"} and captured.get("capture_revision") != 2:
            raise ValueError("resource-v2 descriptor requires the exact capture2 declaration")
        if primary_input_plan_sha256 is not None and (captured.get("capture_revision") != 2
                or type(primary_input_plan_sha256) is not str
                or len(primary_input_plan_sha256) != 64
                or re.fullmatch(r"[a-f0-9]{64}", primary_input_plan_sha256) is None):
            raise ValueError("planned descriptor requires capture2 and exact plan SHA")
        _schedule_datetime("resource due_at", resource_job.due_at)
        if type(resource_job.priority) is not int or not -2**31 <= resource_job.priority < 2**31:
            raise ValueError("resource priority must fit PostgreSQL integer")
        if retained_stage is not None:
            if primary_input_plan_sha256 is None:
                raise ValueError('retained stage requires a registered native plan')
            retained_stage, _ = _submission_part_codec(retained_stage, _AdmissionBudget())
        request = self._candidate_stage_request(resource_job, captured,
            primary_input_plan_sha256=primary_input_plan_sha256, retained_stage=retained_stage)
        _bounded_schedule_text("candidate key", request.schedule_key, 1024)
        detached, _ = _bounded_schedule_parameters(request.parameters)
        return ScheduledOccurrence(request.schedule_key, request.scheduled_for, request.kind,
            detached, request.due_at, request.priority, request.max_attempts, request.deadline_at)

    def enqueue_candidate(self, context: JobContext, checkpoint: Mapping[str, Any], *, primary_input_plan_sha256=None):
        """Return the actual durable stage job from its owning enqueue operation."""
        request = self.candidate_stage_request(context.job, checkpoint,
            primary_input_plan_sha256=primary_input_plan_sha256)
        return self._enqueue_stage_request(context, request)

    def _enqueue_stage_request(self, context, request):
        return self._jobs.enqueue_child(context.lease, child_key=request.schedule_key,
            scheduled_for=request.scheduled_for, kind=request.kind, parameters=request.parameters,
            due_at=request.due_at, priority=request.priority, now=self._clock)

    @staticmethod
    def _candidate_stage_request(resource_job, checkpoint, *, primary_input_plan_sha256=None, retained_stage=None):
        roots = dict(checkpoint["roots"])
        contract = ({"roots": roots, "capture_revision": 2,
                     "root_deliveries": checkpoint["root_deliveries"]}
                    if checkpoint.get("capture_revision") == 2 else roots)
        planned = ({} if primary_input_plan_sha256 is None else
                   dict(primary_input_revision=1, primary_input_plan_sha256=primary_input_plan_sha256))
        if planned:
            contract = {**contract, **planned}
        retained = {} if retained_stage is None else dict(retained_stage=retained_stage)
        if retained:
            contract = {**contract, **retained}
        stage_kind = 'sec-candidate-stage' if not retained else 'sec-retained-candidate-stage-v1'
        identity = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ScheduledOccurrence(
            schedule_key=f"{stage_kind}:{checkpoint['cik']}:{checkpoint['accession']}:{identity}",
            scheduled_for=resource_job.due_at,
            kind=stage_kind,
            parameters={
                **resource_job.parameters,
                "roots": roots,
                **planned,
                **retained,
                **({"capture_revision": 2, "root_deliveries": checkpoint["root_deliveries"]}
                   if checkpoint.get("capture_revision") == 2 else {}),
            },
            due_at=resource_job.due_at,
            priority=resource_job.priority,
        )


@dataclass(frozen=True)
class _Leaf:
    document: str
    payload: bytes
    artifact_sha256: str


class SecCandidateStageHandler:
    def __init__(
        self,
        *,
        edgar: EdgarClient,
        object_store: ImmutableObjectStore,
        artifacts: ArtifactRepository,
        observations: SourceObservationRepository,
        companies: SharedCompanyRepository,
        derive: Callable[[EvidenceBundle], tuple[str, dict | None]] = sync._derive_evidence,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        history: FigureHistoryRepository | None = None,
    ) -> None:
        self._edgar = edgar
        self._object_store = object_store
        self._artifacts = artifacts
        self._observations = observations
        self._companies = companies
        self._derive = derive
        self._clock = clock
        self._history = history

    def __call__(self, context: JobContext) -> Mapping[str, Any]:
        accession, cik, form, filename, observation_id, root_hashes = (
            _candidate_parameters(context.job.parameters)
        )
        strict_retained = context.job.kind == "sec-retained-candidate-stage-v1"
        if strict_retained != ("retained_stage" in context.job.parameters):
            raise ValueError("retained envelope requires its distinct strict stage kind")
        retained_graph = None
        annotated = context.job.parameters.get("capture_revision") == 2
        engine_identity = None
        if annotated:
            if self._history is None or self._history.engine is not self._companies.engine:
                raise ValueError("annotated stage requires same-engine exact history owner")
            completed = self._paired_checkpoint(context, root_hashes)
            if completed is not None:
                return completed
            if not strict_retained:
                engine_identity = self._engine_identity()
        completed = _candidate_checkpoint(
            None if annotated else context.job.checkpoint,
            accession=accession,
            cik=cik,
            observation_id=observation_id,
            roots=root_hashes,
        )
        if completed is not None:
            return completed

        if not strict_retained:
            parent = self._observations.latest_sec_filing(observation_id)
            _validate_parent_filing(
                parent,
                accession=accession,
                cik=cik,
                form=form,
                filename=filename,
                observation_id=observation_id,
            )
        if strict_retained:
            native = primary_input_transport.PrimaryInputTransport(self._artifacts, self._object_store,
                self._observations).resolve_registered_native(context.job.parameters["primary_input_plan_sha256"],
                cik=cik, company_facts_occurrence_id=context.job.parameters["root_deliveries"]["company_facts"]["occurrence_id"],
                security_identifier=f"sec:{cik}:primary-listed-security", retained_stage=context.job.parameters)
            retained_graph, planned = native.retained_graph, native.resolved
            parent = retained_graph.parent
            roots = {role: retained_graph.bodies[source.observation.artifact_sha256]
                     for role, source in retained_graph.root_sources.items() if role != "complete_submission"}
            root_artifacts = tuple(SnapshotArtifact(source.observation.artifact_sha256, "sec_root_" + role)
                for role, source in retained_graph.root_sources.items())
            captured_sources = retained_graph.sources
            inventory, submissions = retained_graph.inventory, retained_graph.submissions
            facts = planned.original_company_facts
            retained_graph.budget.json_decode(len(retained_graph.execution))
            engine_identity = json.loads(retained_graph.execution)["engine_identity_sha256"]
        else:
            verified_payloads = {} if "primary_input_revision" in context.job.parameters else None
            roots, root_artifacts, captured_sources = self._load_roots(
                root_hashes,
                parent=parent,
                accession=accession,
                cik=cik,
                deliveries=context.job.parameters.get("root_deliveries"), verified_payloads=verified_payloads,
            )
            inventory = _strict_json_object(
                roots["accession_inventory"], "SEC accession inventory"
            )
            submissions = _strict_json_object(roots["submissions"], "SEC submissions")
            planned = None
            if verified_payloads is not None:
                planned = primary_input_transport.PrimaryInputTransport(
                    self._artifacts, self._object_store, self._observations).resolve(
                    context.job.parameters["primary_input_plan_sha256"], cik=cik,
                    company_facts_source=next(source for source in captured_sources if source.role == "company_facts"),
                    company_facts_payload=roots["company_facts"],
                    security_identifier=f"sec:{cik}:primary-listed-security", payloads=verified_payloads)
                facts = planned.original_company_facts
                captured_sources = _combined_sources(captured_sources, planned.captured_sources)
            else:
                facts = (sec_json.decode_company_facts(roots["company_facts"],
                            source=next(s for s in captured_sources if s.role == "company_facts"))
                         if annotated else _strict_json_object(roots["company_facts"], "SEC Company Facts"))
            if annotated and (not _same_cik(facts.get("cik"), cik)
                              or not _same_cik(submissions.get("cik"), cik)):
                raise ValueError("annotated captured bodies identify another SEC issuer")
        original_facts = facts if annotated else None
        filing_date = str(parent.observation.canonical_metadata["filing_date"])
        annual, historical_artifact, historical_filings = self._filing_metadata(
            context,
            submissions,
            accession=accession,
            cik=cik,
            expected_form=form,
            filing_date=filing_date, retained_graph=retained_graph,
        )
        names = _inventory_names(inventory, accession=accession, cik=cik)
        primary = annual["document"]
        if primary not in names:
            context.record_item_outcome(
                item_key="graph:primary_document",
                status="failed",
                outcome={"state": "pending", "document": primary},
                error_summary="SEC primary filing document is not in the inventory",
            )
            raise SecResourcePending("SEC primary filing document is not in the inventory")

        base = sync._archive_base(cik, accession)
        leaf_artifacts: list[SnapshotArtifact] = []
        if historical_artifact is not None:
            leaf_artifacts.append(historical_artifact)
        primary_leaf = self._fetch_leaf(
            context,
            accession=accession,
            cik=cik,
            document=primary,
            role="primary_document",
            base=base, retained_graph=retained_graph,
        )
        leaf_artifacts.append(
            SnapshotArtifact(primary_leaf.artifact_sha256, "sec_primary_document")
        )
        cover_leaves = []
        for report in sorted(
            name for name in names if re.fullmatch(r"R[1-3]\.htm", name, re.I)
        ):
            leaf = self._fetch_leaf(
                context,
                accession=accession,
                cik=cik,
                document=report,
                role=f"cover_{report[:-4].casefold()}",
                base=base, retained_graph=retained_graph,
            )
            cover_leaves.append(leaf)
            leaf_artifacts.append(
                SnapshotArtifact(leaf.artifact_sha256, f"sec_cover_{report[:-4].casefold()}")
            )
        cover_leaves.append(primary_leaf)

        inline_artifacts: list[SnapshotArtifact] = []
        if form.split("/", 1)[0] in {"20-F", "40-F"}:
            facts, inline_artifacts = self._foreign_facts(
                context,
                cik=cik,
                annual=annual,
                annual_inventory=inventory,
                annual_inventory_payload=roots["accession_inventory"],
                annual_primary=primary_leaf,
                submissions=submissions,
                historical_filings=historical_filings,
                company_facts=facts,
            )
        else:
            inline_artifacts = self._domestic_inline_leaves(
                context,
                cik=cik,
                accession=accession,
                inventory=inventory,
                primary=primary_leaf, retained_graph=retained_graph,
            )

        ticker, exchange, receipt = _security_identity(
            submissions,
            accession=accession,
            form=form,
            cover_payloads=[leaf.payload for leaf in cover_leaves],
        )
        title = str((receipt or {}).get("title") or "")
        depositary = cover.is_depositary_security(title)
        ratio = (receipt or {}).get("ratio")
        if depositary and ratio is None:
            raise ValueError("SEC depositary security has no filing-backed ratio")
        bundle = EvidenceBundle(cik, ticker, facts, None, receipt,
                                captured_sources=captured_sources, original_company_facts=original_facts,
                                primary_inputs=() if planned is None else planned.outcomes)
        result = sync._derive_evidence_result(bundle) if annotated else None
        status, payload = ((result.status, result.row) if annotated else self._derive(bundle))
        if status == "pending_facts":
            raise SecResourcePending("SEC filing evidence is still pending")
        if status != "ok" or payload is None:
            raise ValueError(f"SEC filing evidence derived as {status}, not ok")

        values = dict(
            issuer_source="SEC",
            issuer_identifier=cik,
            security_identifier=f"sec:{cik}:primary-listed-security",
            ticker=ticker,
            exchange_code=exchange,
            quote_currency="USD",
            security_title=title or None,
            source_accession=accession if title else None,
            security_basis=(
                "PRIMARY_DEPOSITARY_RECEIPT"
                if depositary
                else "PRIMARY_COMMON_SHARE"
                if title
                else "SEC_TICKER_MAPPING_ONLY"
            ),
            receipt_ratio=ratio,
            engine_revision=store.ENGINE_VERSION,
            canonical_payload=payload,
            artifacts=(*root_artifacts, *leaf_artifacts, *inline_artifacts,
                       *(() if planned is None else planned.artifacts)),
        )
        if annotated:
            return self._store_pair(context, values, result, captured_sources, engine_identity, original_facts, planned=planned, retained_graph=retained_graph)
        stored = self._companies.store_candidate(context.lease, **values)
        checkpoint = {
            "accession": accession,
            "cik": cik,
            "filing_observation_id": observation_id,
            "roots": root_hashes,
            "snapshot_id": stored.snapshot_id,
            "snapshot_created": stored.created,
        }
        context.save_checkpoint(checkpoint)
        return checkpoint

    @staticmethod
    def _engine_identity():
        from .captured_assembly import engine_identity
        return engine_identity()

    def _request_identity(self, context, engine_identity):
        return self._request_identity_for_job(context.job, engine_identity)

    @staticmethod
    def _request_identity_for_job(job, engine_identity):
        return hashlib.sha256(json.dumps(dict(parameters=dict(job.parameters),
            job_id=job.job_id, engine_revision=store.ENGINE_VERSION,
            engine_identity_sha256=engine_identity), sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def _store_pair(self, context, values, result, captured_sources, engine_identity, original_facts, *, planned=None, retained_graph=None):
        if retained_graph is not None:
            _reserve_retained_annual_keys(result, retained_graph.budget)
        prepared = exact_annual.prepare(result, captured_sources, cik=values["issuer_identifier"],
                                       original_facts=original_facts)
        if retained_graph is None:
            if self._engine_identity() != engine_identity:
                raise ValueError("engine source changed during annotated derivation")
            request = self._request_identity(context, engine_identity)
        else:
            token = _SUBMISSION_PART_FINGERPRINT_BUDGET.set(retained_graph.budget)
            try:
                if self._engine_identity() != engine_identity:
                    raise ValueError("engine source changed during retained derivation")
            finally:
                _SUBMISSION_PART_FINGERPRINT_BUDGET.reset(token)
            self._reserve_retained_request(context.job, retained_graph.budget, engine_identity)
            request = self._request_identity(context, engine_identity)
        run_key = f"sec-annual:{request}"
        with self._companies.engine.begin() as connection:
            if retained_graph is not None:
                primary_input_transport.PrimaryInputTransport(self._artifacts, self._object_store,
                    self._observations)._recheck_retained_graph(retained_graph, context.job.parameters, connection=connection)
            stored = self._companies.store_candidate_on_connection(connection, context.lease,
                **values, **({} if retained_graph is None else {'_raw_work_budget': retained_graph.budget}))
            if planned is not None and stored.security_id != planned.security_id:
                raise ValueError("stored security differs from registered private plan")
            bundle = prepared.bind(stored, pipeline_run_key=run_key,
                security_basis=values["security_basis"], receipt_ratio=values["receipt_ratio"])
            bundle_id = self._history.store_on_connection(connection, context.lease,
                request_key=run_key, bundle=bundle,
                **({} if retained_graph is None else {'_raw_work_budget': retained_graph.budget}))
            header = self._history.publication_identity_on_connection(connection, bundle_id,
                security_id=stored.security_id, snapshot_id=stored.snapshot_id, expected_run_key=run_key,
                **({} if retained_graph is None else {'_raw_work_budget': retained_graph.budget}))
            checkpoint = dict(checkpoint_revision=2 if planned is None else 3, request_identity=request,
                parameters=dict(context.job.parameters), engine_revision=stored.engine_revision,
                engine_identity_sha256=engine_identity, pipeline_run_key=run_key,
                snapshot_id=stored.snapshot_id, snapshot_sha256=stored.snapshot_sha256,
                security_id=stored.security_id, exact_bundle_id=bundle_id,
                exact_bundle_sha256=header["exact_bundle_sha256"], snapshot_created=stored.created)
            if planned is not None:
                checkpoint["primary_input_transport"] = planned.manifest
            if retained_graph is not None:
                size = _reserve_retained_schedule_codec(checkpoint, retained_graph.budget,
                                                              maximum_bytes=1024 * 1024)
                retained_graph.budget.reserve(work=6 * size)
                primary_input_transport.PrimaryInputTransport(self._artifacts, self._object_store,
                    self._observations)._recheck_retained_graph(retained_graph, context.job.parameters, connection=connection)
            # This is the final potentially waiting write/fence before caller commit.
            context.save_checkpoint_on_connection(connection, checkpoint,
                **({} if retained_graph is None else {"_raw_work_budget": retained_graph.budget}))
        return checkpoint

    def _paired_checkpoint(self, context, roots):
        return self.recover_paired_job(context.job, roots)

    def _paired_checkpoint_identity(self, job, *, strict=False):
        checkpoint = job.checkpoint
        if checkpoint is None:
            return None
        expected = {"checkpoint_revision", "request_identity", "parameters", "engine_revision",
                    "engine_identity_sha256", "pipeline_run_key", "snapshot_id", "snapshot_sha256",
                    "security_id", "exact_bundle_id", "exact_bundle_sha256", "snapshot_created"}
        planned_request = "primary_input_revision" in job.parameters
        if planned_request:
            expected.add("primary_input_transport")
        if strict:
            if planned_request:
                raise ValueError("pair references require an unplanned capture2 stage")
            parameters, canonical = _bounded_schedule_parameters(job.parameters)
            copied, _ = _bounded_schedule_parameters(checkpoint)
            _candidate_parameters(parameters)
            _capture_id("filing observation", parameters["observation_id"])
            if parameters.get("capture_revision") != 2 or type(parameters.get("capture_revision")) is not int:
                raise ValueError("pair references require annotated capture2")
            if set(copied) != expected or _bounded_schedule_parameters(copied["parameters"])[1] != canonical:
                raise ValueError("paired SEC checkpoint differs from actual canonical parameters")
            for name in ("snapshot_id", "security_id", "exact_bundle_id", "engine_revision"):
                _capture_id(name, copied[name])
            for name in ("request_identity", "engine_identity_sha256", "snapshot_sha256", "exact_bundle_sha256"):
                _capture_sha(copied[name])
            _bounded_schedule_text("pair run key", copied["pipeline_run_key"], 4096)
        identity = self._engine_identity()
        request = self._request_identity_for_job(job, identity)
        if (not isinstance(checkpoint, Mapping) or set(checkpoint) != expected
                or type(checkpoint["checkpoint_revision"]) is not int or checkpoint["checkpoint_revision"] != (3 if planned_request else 2)
                or checkpoint["parameters"] != dict(job.parameters)
                or type(checkpoint["engine_revision"]) is not int
                or checkpoint["engine_revision"] != store.ENGINE_VERSION
                or checkpoint["engine_identity_sha256"] != identity
                or checkpoint["request_identity"] != request
                or checkpoint["pipeline_run_key"] != f"sec-annual:{request}"
                or type(checkpoint["snapshot_created"]) is not bool
                or any(type(checkpoint[name]) is not int or checkpoint[name] <= 0
                       for name in ("snapshot_id", "security_id", "exact_bundle_id"))):
            raise ValueError("paired SEC checkpoint identifies a different request or engine")
        return checkpoint, planned_request, identity

    def recover_completed_pair_references(self, *, job_id, occurrence_id, schedule_key, scheduled_for):
        """Recover sealed pair associations without loading snapshot or exact graph payloads."""
        _capture_id("resource job", job_id); _capture_id("resource occurrence", occurrence_id)
        key = _bounded_schedule_text("resource schedule key", schedule_key, 1024)
        if type(scheduled_for) is not datetime:
            raise ValueError("resource scheduled_for must be an exact aware datetime")
        scheduled = _schedule_datetime("resource scheduled_for", scheduled_for)
        if (self._history is None or self._history.engine is not self._companies.engine
                or self._observations.engine is not self._companies.engine
                or self._artifacts.engine is not self._companies.engine):
            raise ValueError("pair reference recovery requires same-engine retained owners")
        jobs = self._companies._jobs
        if jobs.engine is not self._companies.engine:
            raise ValueError("pair reference job owner has a different engine")
        identity = dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=key, scheduled_for=scheduled)
        original = jobs.scheduled_job(**identity)
        resource_stamp = _capture_job_stamp(original)
        executing_identity = self._engine_identity()
        resource = SecResourceFetchHandler(edgar=self._edgar, object_store=self._object_store,
            artifacts=self._artifacts, observations=self._observations, jobs=jobs, clock=self._clock)
        capture = resource.recover_completed_capture(**identity)
        stage_request = stage = stage_stamp = None
        flags = dict(root_capture_bytes_verified=capture["status"] == "VERIFIED",
            snapshot_payload_verified=False, snapshot_leaf_bytes_verified=False, exact_graph_verified=False,
            graph_digest_recomputed=False, financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED")
        reference = dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=key, scheduled_for=scheduled.isoformat())

        def finish(result):
            if _capture_job_stamp(jobs.scheduled_job(**identity)) != resource_stamp:
                raise ValueError("resource changed during pair reference recovery")
            if stage_request is not None:
                actual = jobs.job_for_occurrence(**vars(stage_request))
                if (actual is None) != (stage is None) or (actual is not None and _capture_job_stamp(actual) != stage_stamp):
                    raise ValueError("stage association changed during pair reference recovery")
            if self._engine_identity() != executing_identity:
                raise ValueError("executing source changed during pair reference recovery")
            value = dict(operation_revision=1, resource=reference, **flags, **result)
            detached, size, exceeded = _capture_result_json(value)
            if exceeded:
                value = dict(operation_revision=1, resource=reference, **flags, status="CAPACITY_UNAVAILABLE",
                    gap=dict(phase="result_references", required_bytes=size, limit_bytes=_CAPTURE_REFERENCE_BYTES,
                             required_is_lower_bound=True))
                detached, _, exceeded = _capture_result_json(value)
                if exceeded:
                    raise ValueError("compact pair reference result exceeds its ceiling")
            return detached

        if capture["status"] != "VERIFIED":
            return finish(dict(status="CAPTURE_UNAVAILABLE", capture_status=capture["status"]))
        refreshed = jobs.scheduled_job(**identity)
        if (refreshed.status != "succeeded" or _capture_job_stamp(refreshed) != resource_stamp
                or _bounded_schedule_parameters(refreshed.checkpoint)[1]
                    != _bounded_schedule_parameters(capture["checkpoint"])[1]):
            raise ValueError("verified capture differs from actual completed resource")
        stage_request = resource.candidate_stage_request(refreshed, capture["checkpoint"])
        stage = jobs.job_for_occurrence(**vars(stage_request))
        if stage is None:
            return finish(dict(status="ABSENT_STAGE_ASSOCIATION"))
        stage_stamp = _capture_job_stamp(stage)
        stage_ref = {name: getattr(stage, name) for name in ("job_id", "occurrence_id", "kind", "status", "ownership_generation")}
        if stage.kind != "sec-candidate-stage":
            raise ValueError("pair reference stage has a different kind")
        if stage.status in {"pending", "running", "retry_wait", "failed"}:
            return finish(dict(status="STAGE_FAILED" if stage.status == "failed" else "STAGE_NOT_COMPLETED", stage=stage_ref))
        if stage.status != "succeeded":
            raise ValueError("invalid stage status")
        admitted = self._paired_checkpoint_identity(stage, strict=True)
        if admitted is None:
            raise ValueError("succeeded stage has no paired checkpoint")
        checkpoint, _, pair_identity = admitted
        if pair_identity != executing_identity:
            raise ValueError("paired stage belongs to a different executing source")
        sources = tuple((root["source_item_id"], root["source_observation_id"], root["delivery"]["occurrence_id"])
                        for root in capture["roots"])
        root_links = {(root["artifact"]["content_sha256"], "sec_root_" + root["role"]) for root in capture["roots"]}
        admitted_references = False
        try:
            with self._companies.engine.connect() as connection:
                snapshot = self._companies.snapshot_reference_on_connection(connection, checkpoint["snapshot_id"])
                header = snapshot["header"]
                if (header["security_id"] != checkpoint["security_id"]
                        or header["snapshot_sha256"] != checkpoint["snapshot_sha256"]
                        or header["engine_revision"] != store.ENGINE_VERSION
                        or header["issuer_source"] != "SEC" or header["issuer_identifier"] != stage.parameters["cik"]
                        or header["source_accession"] not in {None, stage.parameters["accession"]}
                        or not root_links.issubset({(row["content_sha256"], row["role"]) for row in snapshot["artifacts"]})):
                    raise ValueError("paired snapshot reference differs from original capture")
                arguments = dict(security_id=checkpoint["security_id"], snapshot_id=checkpoint["snapshot_id"],
                    expected_run_key=checkpoint["pipeline_run_key"], stage_job_id=stage.job_id,
                    snapshot_sha256=checkpoint["snapshot_sha256"], engine_revision=store.ENGINE_VERSION,
                    expected_sources=sources)
                history = self._history.pair_reference_on_connection(connection, checkpoint["exact_bundle_id"], **arguments)
                if history["header"]["bundle_sha256"] != checkpoint["exact_bundle_sha256"]:
                    raise ValueError("paired history reference digest differs from actual checkpoint")
                admitted_references = True
                if (self._companies.snapshot_reference_on_connection(connection, checkpoint["snapshot_id"]) != snapshot
                        or self._history.pair_reference_on_connection(connection, checkpoint["exact_bundle_id"], **arguments) != history):
                    raise ValueError("paired metadata changed during reference recovery")
        except PairReferenceCapacity as error:
            if admitted_references:
                raise ValueError("paired metadata changed during reference recovery") from error
            return finish(dict(status="CAPACITY_UNAVAILABLE", stage=stage_ref,
                gap=dict(phase=error.phase, required=error.required, limit=error.limit, required_is_lower_bound=False)))
        return finish(dict(status="PAIR_REFERENCES_VERIFIED", stage=stage_ref, snapshot=snapshot, exact=history))

    def recover_paired_job(self, job, roots):
        if job.kind == 'sec-retained-candidate-stage-v1':
            return self._recover_retained_pair(job, roots)
        if 'retained_stage' in job.parameters:
            raise ValueError('retained checkpoint requires its distinct strict kind')
        admitted = self._paired_checkpoint_identity(job)
        if admitted is None:
            return None
        checkpoint, planned_request, identity = admitted
        parameters = job.parameters
        parent = self._observations.sec_filing(parameters["observation_id"])
        _validate_parent_filing(parent, accession=parameters["accession"], cik=parameters["cik"],
            form=parameters["form"], filename=parameters["filename"], observation_id=parameters["observation_id"])
        verified_payloads = {} if planned_request else None
        root_payloads, root_artifacts, sources = self._load_roots(roots, parent=parent,
            accession=parameters["accession"], cik=parameters["cik"], deliveries=parameters["root_deliveries"],
            verified_payloads=verified_payloads)
        planned = None
        if planned_request:
            planned = primary_input_transport.PrimaryInputTransport(self._artifacts, self._object_store, self._observations).resolve(
                parameters["primary_input_plan_sha256"], cik=parameters["cik"],
                company_facts_source=next(source for source in sources if source.role == "company_facts"),
                company_facts_payload=root_payloads["company_facts"], security_id=checkpoint["security_id"], payloads=verified_payloads)
            if _canonical_json(planned.manifest) != _canonical_json(checkpoint["primary_input_transport"]):
                raise ValueError("planned SEC checkpoint dependency or admission differs")
            sources = _combined_sources(sources, planned.captured_sources)
        selected = self._companies.snapshot(checkpoint["snapshot_id"])
        if (selected is None or selected.snapshot.security_id != checkpoint["security_id"]
                or selected.snapshot.snapshot_sha256 != checkpoint["snapshot_sha256"]
                or selected.snapshot.engine_revision != store.ENGINE_VERSION
                or selected.issuer_source != "SEC" or selected.issuer_identifier != parameters["cik"]
                or not set(root_artifacts) <= set(selected.artifacts)):
            raise ValueError("paired SEC checkpoint snapshot differs from immutable capture")
        if planned is not None and not set(planned.artifacts).issubset(selected.artifacts):
            raise ValueError("paired SEC checkpoint plan dependencies differ from snapshot")
        for link in selected.artifacts:
            read_registered_artifact(link.content_sha256, self._object_store, self._artifacts,
                                     verified_payloads=verified_payloads)
        with self._companies.engine.connect() as connection:
            header = self._history.publication_identity_on_connection(connection, checkpoint["exact_bundle_id"],
                security_id=checkpoint["security_id"], snapshot_id=checkpoint["snapshot_id"],
                expected_run_key=checkpoint["pipeline_run_key"])
            graph = self._history.load_on_connection(connection, checkpoint["exact_bundle_id"])
        if (header["exact_bundle_sha256"] != checkpoint["exact_bundle_sha256"]
                or graph.snapshot_sha256 != checkpoint["snapshot_sha256"]
                or graph.engine_revision != store.ENGINE_VERSION
                or {(s.source_item_id, s.source_observation_id, s.occurrence_id) for s in graph.sources}
                   != {(s.source_item_id, s.source_observation_id, s.occurrence_id) for s in sources}):
            raise ValueError("paired SEC checkpoint exact graph differs")
        return dict(checkpoint)

    def _retained_leaf(self, graph, *, document, role):
        document = inline_xbrl.safe_document_name(document)
        choices = [leaf for leaf in graph.leaves if leaf.document == document]
        if len(choices) != 1 or role not in choices[0].projected_roles:
            raise ValueError("stage leaf name/role differs from its internally authenticated graph")
        leaf = choices[0]
        body = graph.bodies.get(leaf.artifact.content_sha256)
        if type(body) is not bytes or len(body) != leaf.artifact.byte_size:
            raise ValueError("stage leaf has no private verified immutable bytes")
        return _Leaf(document, body, leaf.artifact.content_sha256)


    def _recover_retained_pair(self, job, roots):
        parameters = job.parameters
        if job.kind != "sec-retained-candidate-stage-v1" or "retained_stage" not in parameters:
            raise ValueError("retained recovery requires its strict actual producer kind")
        if job.checkpoint is None:
            return None
        transport = primary_input_transport.PrimaryInputTransport(self._artifacts, self._object_store, self._observations)
        graph = transport._admit_registered_native_graph(parameters["primary_input_plan_sha256"],
            cik=parameters["cik"], company_facts_occurrence_id=parameters["root_deliveries"]["company_facts"]["occurrence_id"],
            security_identifier=f"sec:{parameters['cik']}:primary-listed-security", parameters=parameters)
        _reserve_retained_schedule_codec(dict(job.checkpoint), graph.budget, maximum_bytes=1024 * 1024)
        graph.budget.json_decode(len(graph.execution))
        self._reserve_retained_request(job, graph.budget, json.loads(graph.execution)["engine_identity_sha256"])
        _, actual_parameters = _retained_schedule_codec(dict(job.checkpoint["parameters"]), graph.budget)
        _, request_parameters = _retained_schedule_codec(dict(job.parameters), graph.budget)
        graph.budget.equality(len(actual_parameters), len(request_parameters))
        if actual_parameters != request_parameters:
            raise ValueError("retained checkpoint canonical parameter identity differs")
        token = _SUBMISSION_PART_FINGERPRINT_BUDGET.set(graph.budget)
        try:
            admitted = self._paired_checkpoint_identity(job)
        finally:
            _SUBMISSION_PART_FINGERPRINT_BUDGET.reset(token)
        if admitted is None:
            raise ValueError("retained checkpoint disappeared")
        checkpoint, planned_request, identity = admitted
        if not planned_request:
            raise ValueError("retained recovery requires its actual planned checkpoint")
        _reserve_retained_schedule_codec(dict(checkpoint), graph.budget, maximum_bytes=1024 * 1024)
        actual = checkpoint["primary_input_transport"]
        if type(actual) is not dict or set(actual) != {"primary_input_revision", "primary_input_plan_sha256",
                "dependencies", "admission", "financial_consumer", "native_history", "retained_stage"}:
            raise ValueError("retained checkpoint manifest differs")
        source_manifest = dict(primary_input_revision=1, primary_input_plan_sha256=graph.plan_sha256,
            dependencies=list(graph.dependency_manifest), financial_consumer="NOT_IMPLEMENTED",
            native_history="NOT_ASSESSED", retained_stage=graph.retained_manifest)
        _, actual_source = _retained_schedule_codec({key: value for key, value in actual.items() if key != "admission"}, graph.budget)
        _, expected_source = _retained_schedule_codec(source_manifest, graph.budget)
        graph.budget.equality(len(actual_source), len(expected_source))
        if actual_source != expected_source:
            raise ValueError("retained checkpoint source/dependency/origin manifest differs")
        admissions = actual["admission"]
        if (type(admissions) is not list or len(admissions) != len(graph.plan.declarations)
                or any(type(value) is not dict or set(value) != {"key", "status", "reason"}
                       or value["key"] != declaration.key or value["status"] not in {"ADMITTED", "UNAVAILABLE"}
                       or value["status"] == "ADMITTED" and value["reason"] is not None
                       for value, declaration in zip(admissions, graph.plan.declarations, strict=True))):
            raise ValueError("historical retained admissions differ from the declared mapping identities")
        with self._companies.engine.connect() as connection:
            selected = self._companies.snapshot_reference_on_connection(connection, checkpoint["snapshot_id"],
                _raw_work_budget=graph.budget)
        selected_header = selected["header"]
        selected_links = {SnapshotArtifact(row['content_sha256'], row['role']) for row in selected['artifacts']}
        required = set(graph.links)
        required.add(SnapshotArtifact(next(leaf.artifact.content_sha256 for leaf in graph.leaves
            if leaf.source_role == "primary_document"), "sec_primary_document"))
        for leaf in graph.leaves:
            for role in leaf.projected_roles:
                if role.startswith("cover_"):
                    required.add(SnapshotArtifact(leaf.artifact.content_sha256, "sec_" + role))
                else:
                    required.add(SnapshotArtifact(leaf.artifact.content_sha256, "sec_inline_filing_" + role))
        if (selected is None or selected_header["security_id"] != checkpoint["security_id"]
                or selected_header["snapshot_sha256"] != checkpoint["snapshot_sha256"]
                or selected_header["engine_revision"] != store.ENGINE_VERSION
                or selected_header["issuer_source"] != "SEC" or selected_header["issuer_identifier"] != parameters["cik"]
                or selected_links != required):
            raise ValueError("retained checkpoint snapshot or exact artifact-role membership differs")
        with self._companies.engine.connect() as connection:
            header = self._history.publication_identity_on_connection(connection, checkpoint["exact_bundle_id"],
                security_id=checkpoint["security_id"], snapshot_id=checkpoint["snapshot_id"],
                expected_run_key=checkpoint["pipeline_run_key"], _raw_work_budget=graph.budget)
            sealed = self._history.load_on_connection(connection, checkpoint["exact_bundle_id"], _raw_work_budget=graph.budget)
        if (header["exact_bundle_sha256"] != checkpoint["exact_bundle_sha256"]
                or sealed.snapshot_sha256 != checkpoint["snapshot_sha256"]
                or sealed.engine_revision != store.ENGINE_VERSION
                or {(source.source_item_id, source.source_observation_id, source.occurrence_id) for source in sealed.sources}
                   != {(source.source_item_id, source.source_observation_id, source.occurrence_id) for source in graph.sources}):
            raise ValueError("retained checkpoint sealed pair/source union differs")
        transport._recheck_retained_graph(graph, parameters)
        return dict(checkpoint)


    @staticmethod
    def _reserve_retained_request(job, budget, engine_identity):
        value = dict(parameters=dict(job.parameters), job_id=job.job_id,
            engine_revision=store.ENGINE_VERSION, engine_identity_sha256=engine_identity)
        size = _reserve_retained_schedule_codec(value, budget, maximum_bytes=1024 * 1024)
        budget.reserve(work=2 * size)

    def _load_roots(
        self,
        roots: Mapping[str, str],
        *,
        parent,
        accession: str,
        cik: str,
        deliveries=None,
        verified_payloads=None,
    ) -> tuple[dict[str, bytes], tuple[SnapshotArtifact, ...], tuple[CapturedSource, ...]]:
        payloads = {}
        artifacts = []
        sources = []
        for role, digest in roots.items():
            stored = _retained_root_source(self._observations, digest,
                role=role, parent=parent, accession=accession, cik=cik)
            item = stored.item
            observation = stored.observation
            artifact, payload = read_registered_artifact(observation.artifact_sha256,
                self._object_store, self._artifacts, verified_payloads=verified_payloads)
            if role != "complete_submission":
                payloads[role] = payload
            artifacts.append(
                SnapshotArtifact(artifact.content_sha256, f"sec_root_{role}")
            )
            if deliveries is not None:
                supplied = deliveries[role]
                delivery = _retained_root_delivery(self._observations, stored, supplied, compare_instants=True)
                sources.append(CapturedSource(role, artifact.content_sha256, observation.source_url,
                    item.source_item_id, observation.source_observation_id, delivery.occurrence_id, "EXACT_LEXICAL"))
        return payloads, tuple(artifacts), tuple(sources)

    def _filing_metadata(
        self,
        context: JobContext,
        submissions: Mapping[str, Any],
        *,
        accession: str,
        cik: str,
        expected_form: str,
        filing_date: str,
        retained_graph=None,
    ) -> tuple[
        dict[str, str], SnapshotArtifact | None, Mapping[str, Any] | None
    ]:
        found = _recent_filing_metadata(submissions, accession)
        historical_artifact = None
        historical_filings = None
        if found is None and retained_graph is not None:
            raise SecResourcePending('retained recent submissions do not name the filing')
        if found is None:
            name = _historical_submissions_file(submissions, cik, filing_date)
            if name is None:
                raise SecResourcePending("SEC historical submissions metadata is pending")
            historical = self._fetch_leaf(
                context,
                accession=accession,
                cik=cik,
                document=name,
                role="historical_submissions",
                base="https://data.sec.gov/submissions/",
                item_kind="aggregate",
            )
            payload = _strict_json_object(
                historical.payload, "SEC historical submissions"
            )
            found = _recent_filing_metadata(payload, accession, nested=False)
            historical_filings = payload
            historical_artifact = SnapshotArtifact(
                historical.artifact_sha256, "sec_historical_submissions"
            )
        if found is None:
            raise SecResourcePending("SEC submissions metadata does not name the filing")
        if found["form"] != expected_form or found["filed"] != filing_date:
            raise ValueError("SEC submissions filing metadata conflicts with its parent")
        return found, historical_artifact, historical_filings

    def _fetch_leaf(
        self,
        context: JobContext,
        *,
        accession: str,
        cik: str,
        document: str,
        role: str,
        base: str,
        item_kind: str = "resource",
        retained_graph=None,
    ) -> _Leaf:
        if context.stopping.is_set():
            raise SecDiscoveryStopped("SEC leaf fetch stopped before the next resource")
        if retained_graph is not None:
            return self._retained_leaf(retained_graph, document=document, role=role)
        document = inline_xbrl.safe_document_name(document)
        url = base + document
        item = SourceItemIdentity(
            "SEC",
            item_kind,
            (
                f"submissions/{cik}/{document}"
                if item_kind == "aggregate"
                else f"{accession}/{document}"
            ),
            issuer_source_identifier=cik,
            parent_source_item_id=None,
        )
        metadata = {
            "role": role,
            "cik": cik,
            "document": document,
        }
        if item_kind != "aggregate":
            metadata["accession"] = accession
        try:
            result = self._edgar.fetch(url)
        except NoXbrlDataError as error:
            self._observations.record(
                context.lease,
                item=item,
                item_outcome_key=f"leaf:{accession}:{document}",
                state="pending",
                metadata={**metadata, "reason": "not_found"},
                source_url=url,
                detected_at=self._clock(),
            )
            raise SecResourcePending(f"SEC leaf resource is pending: {document}") from error
        artifact = store_evidence(
            result.data,
            result.media_type or "application/octet-stream",
            self._object_store,
            self._artifacts,
        )
        self._observations.record(
            context.lease,
            item=item,
            item_outcome_key=f"leaf:{accession}:{document}",
            state="present",
            metadata=metadata,
            source_url=result.url,
            artifact_sha256=artifact.content_sha256,
            etag=result.etag,
            last_modified=result.last_modified,
            detected_at=self._clock(),
        )
        return _Leaf(
            document,
            result.data,
            artifact.content_sha256,
        )

    def _domestic_inline_leaves(
        self,
        context: JobContext,
        *,
        cik: str,
        accession: str,
        inventory: Mapping[str, Any],
        primary: _Leaf,
        retained_graph=None,
    ) -> list[SnapshotArtifact]:
        if retained_graph is not None:
            documents = {'primary_document': retained_graph.required.primary}
            for document, roles in retained_graph.required.projections:
                documents.update({role: document for role in roles if not role.startswith('cover_')})
        else:
            try:
                documents = sync._inline_documents(dict(inventory), primary.document)
            except inline_xbrl.UnsupportedInlineXbrlRelationship:
                if _has_inline_companions(inventory):
                    raise SecResourcePending("SEC Inline-XBRL graph is pending")
                return []
            except ValueError as error:
                _raise_if_incomplete_inline_graph(error)
                raise
        base = sync._archive_base(cik, accession)
        by_document = {primary.document: primary}
        artifacts = []
        for role, document in documents.items():
            leaf = by_document.get(document)
            if leaf is None:
                leaf = self._fetch_leaf(
                    context,
                    accession=accession,
                    cik=cik,
                    document=document,
                    role=role,
                    base=base, retained_graph=retained_graph,
                )
                by_document[document] = leaf
            artifacts.append(
                SnapshotArtifact(
                    leaf.artifact_sha256, f"sec_inline_filing_{role}"
                )
            )
        return artifacts

    def _foreign_facts(
        self,
        context: JobContext,
        *,
        cik: str,
        annual: Mapping[str, str],
        annual_inventory: Mapping[str, Any],
        annual_inventory_payload: bytes,
        annual_primary: _Leaf,
        submissions: Mapping[str, Any],
        historical_filings: Mapping[str, Any] | None,
        company_facts: dict[str, Any],
    ) -> tuple[dict[str, Any], list[SnapshotArtifact]]:
        accession = annual["accession"]
        direct_error: ValueError | None = None
        try:
            documents = sync._inline_documents(
                dict(annual_inventory), annual_primary.document
            )
        except inline_xbrl.UnsupportedInlineXbrlRelationship as error:
            if _has_inline_companions(annual_inventory):
                try:
                    inline_xbrl.incorporated_annual_source(
                        annual_primary.payload, cik
                    )
                except inline_xbrl.UnsupportedInlineXbrlRelationship as relation_error:
                    if str(relation_error) == "missing_incorporation_by_reference":
                        raise SecResourcePending(
                            "SEC Inline-XBRL graph is pending"
                        ) from error
                    raise
            direct_error = error
        except ValueError as error:
            _raise_if_incomplete_inline_graph(error)
            raise
        else:
            manifest, payloads, leaves = self._inline_manifest(
                context,
                cik=cik,
                annual=annual,
                source=annual,
                relationship="direct_annual",
                documents=documents,
                index_payload=annual_inventory_payload,
                index_url=sync._archive_base(cik, accession) + "index.json",
                annual_primary=annual_primary,
                entity_name=str(submissions.get("name") or ""),
            )
            supplement = _parse_inline_manifest(manifest, payloads)
            filing, basis = normalize._current_supported_foreign_annual(
                supplement.get("facts") or {}
            )
            expected = (annual["filed"], accession)
            if filing == expected and basis is not None:
                return self._merge_and_retain_manifest(
                    company_facts, manifest, supplement, leaves
                )
            direct_error = ValueError(
                "retained Inline-XBRL statement has no coherent annual balance"
            )

        try:
            relationship = inline_xbrl.incorporated_annual_source(
                annual_primary.payload, cik
            )
        except inline_xbrl.UnsupportedInlineXbrlRelationship as error:
            if str(error) == "missing_incorporation_by_reference" and direct_error:
                raise direct_error from error
            raise
        source = _recent_filing_metadata(submissions, relationship["accession"])
        if source is None and historical_filings is not None:
            source = _recent_filing_metadata(
                historical_filings, relationship["accession"], nested=False
            )
        if source is None:
            raise SecResourcePending("incorporated SEC filing metadata is pending")
        if source["form"].split("/", 1)[0] != "6-K":
            raise ValueError("incorporated SEC source form does not match Form 6-K")
        if source["filed"] > annual["filed"]:
            raise ValueError("incorporated SEC source filing is later than annual wrapper")
        source_base = sync._archive_base(cik, source["accession"])
        source_index = self._fetch_leaf(
            context,
            accession=source["accession"],
            cik=cik,
            document="index.json",
            role="source_index",
            base=source_base,
        )
        source_inventory = _strict_json_object(
            source_index.payload, "SEC incorporated accession inventory"
        )
        _inventory_names(
            source_inventory, accession=source["accession"], cik=cik
        )
        try:
            sync._inline_documents(source_inventory, source["document"])
            documents = sync._inline_documents(
                source_inventory, relationship["document"]
            )
        except inline_xbrl.UnsupportedInlineXbrlRelationship as error:
            raise SecResourcePending(
                "incorporated SEC Inline-XBRL graph is pending"
            ) from error
        except ValueError as error:
            _raise_if_incomplete_inline_graph(error)
            raise
        source = {
            **source,
            "document": documents["primary_document"],
            "filing_document": source["document"],
        }
        manifest, payloads, leaves = self._inline_manifest(
            context,
            cik=cik,
            annual=annual,
            source=source,
            relationship="incorporated_annual_exhibit",
            documents=documents,
            index_payload=source_index.payload,
            index_url=source_base + "index.json",
            annual_primary=annual_primary,
            relationship_evidence=relationship,
            source_index=source_index,
            annual_inventory_payload=annual_inventory_payload,
            entity_name=str(submissions.get("name") or ""),
        )
        supplement = _parse_inline_manifest(manifest, payloads)
        filing, basis = normalize._current_supported_foreign_annual(
            supplement.get("facts") or {}
        )
        if filing != (annual["filed"], accession) or basis is None:
            raise ValueError(
                "retained Inline-XBRL statement has no coherent annual balance"
            )
        return self._merge_and_retain_manifest(
            company_facts, manifest, supplement, leaves
        )

    def _inline_manifest(
        self,
        context: JobContext,
        *,
        cik: str,
        annual: Mapping[str, str],
        source: Mapping[str, str],
        relationship: str,
        documents: Mapping[str, str],
        index_payload: bytes,
        index_url: str,
        annual_primary: _Leaf,
        entity_name: str,
        relationship_evidence: Mapping[str, str] | None = None,
        source_index: _Leaf | None = None,
        annual_inventory_payload: bytes | None = None,
    ) -> tuple[dict[str, Any], dict[str, bytes], list[SnapshotArtifact]]:
        source_accession = source["accession"]
        source_base = sync._archive_base(cik, source_accession)
        by_document: dict[str, _Leaf] = {}
        if source_accession == annual["accession"]:
            by_document[annual_primary.document] = annual_primary
        leaves: list[SnapshotArtifact] = []
        role_leaves: dict[str, _Leaf] = {}
        for role, document in documents.items():
            leaf = by_document.get(document)
            if leaf is None:
                leaf = self._fetch_leaf(
                    context,
                    accession=source_accession,
                    cik=cik,
                    document=document,
                    role=role,
                    base=source_base,
                )
                by_document[document] = leaf
            role_leaves[role] = leaf
            leaves.append(
                SnapshotArtifact(
                    leaf.artifact_sha256,
                    _inline_artifact_role(relationship, role),
                )
            )

        records = {
            role: _manifest_record(
                leaf.document, source_base + leaf.document, leaf.payload
            )
            for role, leaf in role_leaves.items()
        }
        if relationship == "direct_annual":
            records["index"] = _manifest_record(
                "index.json", index_url, index_payload
            )
            leaves.append(
                SnapshotArtifact(
                    hashlib.sha256(index_payload).hexdigest(),
                    "sec_inline_direct_annual_index",
                )
            )
        else:
            assert source_index is not None and annual_inventory_payload is not None
            annual_base = sync._archive_base(cik, annual["accession"])
            records.update(
                {
                    "annual_index": _manifest_record(
                        "index.json",
                        annual_base + "index.json",
                        annual_inventory_payload,
                    ),
                    "annual_primary_document": _manifest_record(
                        annual_primary.document,
                        annual_base + annual_primary.document,
                        annual_primary.payload,
                    ),
                    "source_index": _manifest_record(
                        "index.json", source_base + "index.json", source_index.payload
                    ),
                }
            )
            leaves.extend(
                (
                    SnapshotArtifact(
                        hashlib.sha256(annual_inventory_payload).hexdigest(),
                        "sec_inline_annual_wrapper_index",
                    ),
                    SnapshotArtifact(
                        annual_primary.artifact_sha256,
                        "sec_inline_annual_wrapper_primary_document",
                    ),
                    SnapshotArtifact(
                        source_index.artifact_sha256,
                        "sec_inline_incorporated_source_index",
                    ),
                )
            )
        manifest = {
            "schema": inline_xbrl.MANIFEST_SCHEMA,
            "parser_contract_revision": inline_xbrl.PARSER_CONTRACT_REVISION,
            "cik": cik,
            "entity_name": entity_name,
            "annual": dict(annual),
            "relationship": relationship,
            "source": dict(source),
            "files": records,
        }
        if relationship_evidence is not None:
            manifest["relationship_evidence"] = dict(relationship_evidence)
        return (
            manifest,
            {role: leaf.payload for role, leaf in role_leaves.items()},
            leaves,
        )

    def _merge_and_retain_manifest(
        self,
        company_facts: dict[str, Any],
        manifest: Mapping[str, Any],
        supplement: dict[str, Any],
        leaves: list[SnapshotArtifact],
    ) -> tuple[dict[str, Any], list[SnapshotArtifact]]:
        encoded = inline_xbrl.manifest_bytes(manifest)
        artifact = store_evidence(
            encoded,
            "application/json",
            self._object_store,
            self._artifacts,
        )
        leaves.append(
            SnapshotArtifact(artifact.content_sha256, "sec_inline_current_manifest")
        )
        return inline_xbrl.merge_missing_facts(company_facts, supplement), leaves

def _resource_identity_parameters(parameters):
    accession = parameters["accession"]
    cik = parameters["cik"]
    form = parameters["form"]
    filename = parameters["filename"]
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
    return accession, cik, form, filename


def resource_route(filing: SecIndexFiling) -> tuple[str, str | None]:
    if not filing.form.startswith(_SEC_FINANCIAL_PREFIXES):
        return "NONFINANCIAL_PREFIX", None
    try:
        _resource_identity_parameters(dict(accession=filing.accession, cik=filing.cik,
            form=filing.form, filename=filing.archive_filename))
    except ValueError as error:
        return "UNSUPPORTED_RESOURCE_ROUTE", str(error)
    return "RESOURCE_V2", None


def _finite_transport_texts(result, maximum):
    for name in ("url", "media_type", "etag", "last_modified"):
        value = getattr(result, name)
        if value is not None and (type(value) is not str or "\0" in value or len(value.encode("utf-8")) > maximum):
            raise ValueError(f"finite captured {name} exceeds its text ceiling")


from types import MappingProxyType

MIB = 1024 * 1024


def _size(value):
    if type(value) is not int or value < 0:
        raise ValueError('exact nonnegative size required')
    return value


class _AdmissionCapacity(ValueError):
    pass


@dataclass
class _AdmissionBudget:
    read_bytes: int = 0
    work_bytes: int = 0

    def reserve(self, *, reads=0, work=0):
        reads, work = _size(reads), _size(work)
        _size(self.read_bytes)
        _size(self.work_bytes)
        if self.read_bytes + reads > 64 * MIB:
            raise _AdmissionCapacity("strict cumulative registered-read ceiling")
        if self.work_bytes + work > 128 * MIB:
            raise _AdmissionCapacity("strict cumulative raw-work ceiling")
        self.read_bytes += reads
        self.work_bytes += work

    def artifact_read(self, size):
        size = _size(size)
        self.reserve(reads=size + 2, work=size)

    def receipt_codec(self, value):
        return _reserve_submission_part_receipt(self, phase="codec", raw_value=value)

    def sql_row_preflight(self, identifiers, length_count, maximum_rows=1):
        # Strict callers bound their requested text keys to1024 UTF8 bytes.
        fixed = 64 + 16 * length_count + sum(1024 if getattr(column.type, "python_type", None) is str else 32
                                            for column in identifiers)
        self.reserve(reads=fixed * maximum_rows, work=3 * fixed * maximum_rows)

    def sql_row_read(self, json_lengths, text_lengths):
        json_bytes = sum(_size(size) + 1 for size in json_lengths if size is not None)
        text_bytes = sum(_size(size) for size in text_lengths if size is not None)
        # Psycopg may copy its buffer before UTF8/JSON/numeric materialization.
        self.reserve(reads=json_bytes + text_bytes, work=4 * json_bytes + 2 * text_bytes)

    def receipt_observation(self, observation):
        size, _ = _submission_part_json_size(observation.canonical_metadata, self, 65536)
        _reserve_submission_part_receipt(self, phase="lookup", metadata_bytes=size, observation=observation)

    def json_decode(self, size):
        size = _size(size)
        self.reserve(work=3 * size)

    def native_reader(self, size):
        size = _size(size)
        self.reserve(work=3 * size)

    def document(self, body_size, start, end):
        body_size, start, end = _size(body_size), _size(start), _size(end)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= body_size:
            raise ValueError("document span exceeds admitted artifact")
        self.reserve(work=body_size + 4 * (end - start))

    def equality(self, left_size, right_size):
        left_size, right_size = _size(left_size), _size(right_size)
        self.reserve(work=left_size + right_size)

    def parser(self, input_size, maximum_work):
        input_size, maximum_work = _size(input_size), _size(maximum_work)
        if input_size > 8 * MIB or maximum_work > 48 * MIB:
            raise _AdmissionCapacity("existing submission decoder ceiling")
        self.reserve(work=maximum_work)


def _submission_part_json_size(value, budget, maximum_bytes):
    maximum_bytes = _size(maximum_bytes)
    if not 0 < maximum_bytes <= 65536:
        raise ValueError("strict retained JSON object/frontier required")
    return _schedule_json_size(value, budget, maximum_bytes)


def _schedule_json_size(value, budget, maximum_bytes):
    """Size metadata without encoding or decoding it; the existing codec admits it."""
    maximum_bytes = _size(maximum_bytes)
    if not 0 < maximum_bytes <= 1024 * 1024 or type(value) is not dict:
        raise ValueError("strict retained JSON object/frontier required")
    pending, nodes, encoded_size, utf8_size = [(value, 0)], 0, 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 100000 or depth > 32:
            raise ValueError("existing schedule JSON node/depth ceiling")
        kind = type(item)
        if kind is dict or kind is list:
            count = len(item)
            needed = (2 if kind is dict else 1) * count
            if count > 10000 or len(pending) + needed > 100000 - nodes:
                raise ValueError("existing schedule JSON pending-work ceiling")
            encoded_size += 2 + max(0, count - 1) + (count if kind is dict else 0)
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError("existing schedule JSON exact string key required")
                    pending.extend(((key, depth + 1), (child, depth + 1)))
            else:
                pending.extend((child, depth + 1) for child in item)
        elif kind is str:
            if len(item) > 65536:
                raise ValueError("existing schedule JSON string ceiling")
            budget.reserve(work=4 * len(item))
            encoded_size += 2
            raw_size = 0
            for character in item:
                code = ord(character)
                if 0xD800 <= code <= 0xDFFF:
                    raise ValueError("schedule text cannot encode as UTF-8")
                raw_size += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
                encoded_size += (2 if character in '"\\\b\f\n\r\t' else
                                 6 if code < 32 or 127 <= code <= 65535 else
                                 12 if code > 65535 else 1)
            if raw_size > 65536:
                raise ValueError("existing schedule JSON UTF-8 string ceiling")
            utf8_size += raw_size
        elif kind is int:
            if not -2**63 <= item < 2**63:
                raise ValueError("existing schedule JSON signed64 ceiling")
            budget.reserve(work=20)
            encoded_size += len(str(item))
        elif kind is float:
            if not isfinite(item):
                raise ValueError("existing schedule JSON finite-number requirement")
            budget.reserve(work=32)
            encoded_size += len(repr(item))
        elif kind is bool or item is None:
            encoded_size += 4 if item is None or item is True else 5
        else:
            raise ValueError("existing schedule JSON value required")
        if encoded_size > maximum_bytes:
            raise _AdmissionCapacity("strict retained JSON encoded-byte ceiling")
    return encoded_size, utf8_size


def _reserve_submission_part_generic_codec(value, budget, maximum_bytes=65536):
    encoded_size, utf8_size = _submission_part_json_size(value, budget, maximum_bytes)
    # String validation, JSON emission, chunk UTF-8, canonical join, detached JSON scan.
    budget.reserve(work=utf8_size + 4 * encoded_size)
    return encoded_size


def _submission_part_codec(value, budget, maximum_bytes=65536):
    expected_size = _reserve_submission_part_generic_codec(value, budget, maximum_bytes)
    detached, canonical = _bounded_schedule_parameters(value)
    if type(canonical) is not str or len(canonical) != expected_size or not canonical.isascii():
        raise ValueError("existing schedule codec differs from its metadata size reservation")
    budget.reserve(work=expected_size)
    raw = canonical.encode("utf-8")
    if type(detached) is not dict or len(raw) != expected_size or len(raw) > maximum_bytes:
        raise ValueError("strict retained detached JSON/UTF-8 contract differs")
    return detached, raw


def _read_retained_body(owner, admitted, digest):
    """Internal admitted metadata and immutable cache; no external body credentials."""
    _capture_sha(digest)
    expected = admitted.artifacts.get(digest)
    if expected is None:
        raise ValueError("retained body metadata was not admitted")
    size = _size(expected.byte_size)
    maxima = getattr(admitted, "maxima", None)
    if maxima is None:
        maximum = _CAPTURE_BODY_BYTES
    else:
        if type(maxima) is not MappingProxyType or digest not in maxima:
            raise ValueError("internal immutable admitted body maxima required")
        maximum = _size(maxima[digest])
        if maximum not in {_CAPTURE_BODY_BYTES, _CAPTURE_FACTS_BYTES}:
            raise ValueError("existing ordinary/CF profile maximum required")
    if expected.content_sha256 != digest or expected.object_key != "raw/sha256/" + digest or size > maximum:
        raise ValueError("retained body address/admitted ceiling differs")
    if len(admitted.artifacts) > 64 or sum(_size(record.byte_size) for record in admitted.artifacts.values()) > _CAPTURE_TOTAL_BYTES:
        raise _AdmissionCapacity("existing retained unique artifact/byte ceiling")
    if digest in admitted.bodies:
        body = admitted.bodies[digest]
        if type(body) is not bytes or len(body) != size:
            raise ValueError("private immutable body cache differs from admitted metadata")
        return body
    admitted.budget.artifact_read(size)
    actual, body = read_registered_artifact(digest, owner._object_store, owner._artifacts,
        maximum=maximum, label="retained submission")
    if actual != expected or type(body) is not bytes or len(body) != size:
        raise ValueError("registered body changed after metadata admission")
    admitted.bodies[digest] = body
    return body


def _reserve_submission_part_store(budget, payload_bytes):
    size = _size(payload_bytes)
    if not 0 < size <= _CAPTURE_BODY_BYTES:
        raise _AdmissionCapacity("existing auxiliary ordinary body ceiling")
    # put_verified hashes input and verifies one existing or newly stored object body.
    budget.reserve(reads=size + 2, work=2 * size)


def _reserve_submission_part_receipt(budget, *, phase, raw_value=None, metadata_bytes=None, observation=None):
    if phase not in {"codec", "lookup"}:
        raise ValueError("exact internal receipt operation required")
    if phase == "codec" and (metadata_bytes is not None or observation is not None):
        raise ValueError("receipt codec reservation does not admit a source lookup")
    if phase == "codec" and type(raw_value) is not dict:
        raise ValueError("actual raw receipt is required before its existing decoder")
    if phase != "codec" and raw_value is not None:
        raise ValueError("raw receipt preflight belongs only to its decoder phase")
    if phase == "lookup" and (metadata_bytes is None or observation is None):
        raise ValueError("actual source preflight is required for every receipt lookup")
    envelope = None
    if metadata_bytes is not None:
        metadata_bytes = _size(metadata_bytes)
        if metadata_bytes > 65536 or observation is None:
            raise ValueError("actual source metadata/envelope preflight required before receipt getter")
        published = observation.source_published_at
        published = (None if published is None else
                     _schedule_datetime("source publication", published).isoformat(timespec="microseconds").replace("+00:00", "Z"))
        scalars = dict(state=observation.state, metadata=None,
            artifact_sha256=observation.artifact_sha256, source_url=observation.source_url,
            etag=observation.etag, last_modified=observation.last_modified,
            source_published_at=published)
        scalar_size, scalar_utf8 = _submission_part_json_size(scalars, budget, 65536)
        envelope = scalar_size - len("null") + metadata_bytes
        if envelope > 65536:
            raise _AdmissionCapacity("strict original observation encoded-byte ceiling")
    if phase == "codec":
        receipt_size, receipt_utf8 = _submission_part_json_size(raw_value, budget, 8192)
        budget.reserve(work=2 * receipt_size + receipt_utf8)
    if envelope is not None:
        # acquisition_receipt: detached metadata emission/scan; observation emission/UTF-8/SHA.
        budget.reserve(work=2 * metadata_bytes + 3 * envelope + scalar_utf8)
    if phase == "codec":
        return receipt_size


def _reserve_submission_part_record(budget, metadata_bytes):
    metadata_bytes = _size(metadata_bytes)
    if metadata_bytes > 65536:
        raise _AdmissionCapacity("strict derived observation metadata ceiling")
    # The derived metadata carries the identical container source URL as a nested member.
    envelope = 2 * metadata_bytes + 512
    budget.reserve(work=(2 * metadata_bytes + 2 * metadata_bytes
                         + 3 * envelope + 2 * metadata_bytes
                         + 4 * metadata_bytes + 11 * 8192 + 4 * 256))


def _reserve_submission_part_original_request(budget, actual_resource_job, *, phase, request=None):
    if phase == "stamp":
        _reserve_submission_part_generic_codec(actual_resource_job.parameters, budget)
        if actual_resource_job.checkpoint is not None:
            _reserve_submission_part_generic_codec(actual_resource_job.checkpoint, budget)
        # Existing stamp's bounded kind/error text checks; no stamp hash occurs there.
        for text in (actual_resource_job.kind, actual_resource_job.error_summary):
            if text is not None:
                _, raw_size = _submission_part_json_size(dict(text=text), budget, 65536)
                budget.reserve(work=raw_size)
    elif phase == "partial_request":
        if request is None:
            raise ValueError("original request must be the existing owner's actual constructed record")
        size = _reserve_submission_part_generic_codec(request, budget)
        budget.reserve(work=2 * size)  # Original owner's canonical UTF-8 allocation and SHA.
    else:
        raise ValueError("exact internal original resource phase required")


def _reserve_submission_part_checkpoint(budget, encoded_bytes):
    size = _size(encoded_bytes)
    if size > 65536:
        raise _AdmissionCapacity("strict extraction checkpoint ceiling")
    # Durable save_checkpoint's canonical emission/scan, plus bounded JSONB serialization/UTF-8.
    budget.reserve(work=6 * size)


def _reserve_submission_part_fingerprints(budget, *, engine_file_bytes, producer_file_bytes,
                                         engine_encoding_bytes, producer_encoding_bytes,
                                         producer_import_pending):
    """Internal executing FILE closure sizes; never parameters or request authority."""
    engine_file_bytes, producer_file_bytes = _size(engine_file_bytes), _size(producer_file_bytes)
    engine_encoding_bytes, producer_encoding_bytes = _size(engine_encoding_bytes), _size(producer_encoding_bytes)
    if type(producer_import_pending) is not bool:
        raise ValueError("exact module-import state required")
    if engine_encoding_bytes > 65536 or producer_encoding_bytes > 65536:
        raise _AdmissionCapacity("executing fingerprint metadata frontier")
    files = engine_file_bytes + producer_file_bytes
    if producer_import_pending:
        files += producer_file_bytes
    # Unique files are read/hashed once per pin pair; each owner encodes its own map.
    budget.reserve(reads=files, work=files + 3 * (engine_encoding_bytes + producer_encoding_bytes))

_SUBMISSION_PART_FINGERPRINT_BUDGET = ContextVar("submission_part_fingerprint_budget", default=None)
_SUBMISSION_PART_FINGERPRINT_PAIR = ContextVar("submission_part_fingerprint_pair", default=None)


def _submission_part_fingerprint_size(names, budget):
    # None is a size-only skeleton, never a source hash or admitted build snapshot.
    size, _ = _submission_part_json_size(dict.fromkeys(names), budget, 65536)
    size += 62 * len(names)  # Each actual 64-character SHA string replaces JSON null.
    if size > 65536:
        raise _AdmissionCapacity("executing fingerprint metadata frontier")
    return size


def _submission_part_source_hashes(entries, budget, *, owner_kind, encode_result):
    """Reserve and cap reads from the exact paths selected by the existing owner."""
    if owner_kind not in {"engine", "producer"} or type(encode_result) is not bool:
        raise ValueError("exact private fingerprint operation required")
    names = tuple(name for name, _ in entries)
    if len(set(names)) != len(names):
        raise ValueError("executing fingerprint owner selected duplicate names")
    pair = _SUBMISSION_PART_FINGERPRINT_PAIR.get()
    if pair is not None and pair[0] is not budget:
        raise ValueError("executing fingerprint pair belongs to a different budget")
    hashes = {} if pair is None else pair[1]
    encoded_size = _submission_part_fingerprint_size(names, budget) if encode_result else 0
    files = tuple((name, path, path.stat().st_size) for name, path in entries if path not in hashes)
    read_bytes = 0
    for _, _, size in files:
        if type(size) is not int or size < 0:
            raise ValueError("executing source file size must be an exact nonnegative integer")
        read_bytes += size + 1
    _reserve_submission_part_fingerprints(budget,
        engine_file_bytes=read_bytes if owner_kind == "engine" else 0,
        producer_file_bytes=read_bytes if owner_kind == "producer" else 0,
        engine_encoding_bytes=encoded_size if owner_kind == "engine" else 0,
        producer_encoding_bytes=encoded_size if owner_kind == "producer" else 0,
        producer_import_pending=False)
    for name, path, size in files:
        with path.open("rb") as stream:
            raw = stream.read(size + 1)
        if len(raw) != size:
            raise ValueError("executing source file changed size after reservation")
        hashes[path] = hashlib.sha256(raw).hexdigest()
    return {name: hashes[path] for name, path in entries}


def _submission_part_producer_encoding(current, imported, budget):
    size = _submission_part_fingerprint_size(tuple(current), budget)
    previous_size = _submission_part_fingerprint_size(tuple(imported), budget)
    budget.equality(size, previous_size)
    _reserve_submission_part_fingerprints(budget, engine_file_bytes=0, producer_file_bytes=0,
        engine_encoding_bytes=0, producer_encoding_bytes=size, producer_import_pending=False)

_SUBMISSION_PART_REQUEST_BYTES = 64 * 1024


def _submission_part_request_identity(job, budget):
    """Hash the admitted immutable content of the actual scheduled producer."""
    _capture_id("submission producer job", job.job_id)
    _capture_id("submission producer occurrence", job.occurrence_id)
    _bounded_schedule_text("submission producer kind", job.kind, 256)
    if (type(job.priority) is not int or not -2**31 <= job.priority < 2**31
            or type(job.max_attempts) is not int or not 0 < job.max_attempts < 2**31):
        raise ValueError("submission producer schedule scalars are invalid")
    producer = dict(job_id=job.job_id, occurrence_id=job.occurrence_id, kind=job.kind,
        parameters=job.parameters, priority=job.priority, max_attempts=job.max_attempts,
        due_at=_schedule_datetime("submission producer due_at", job.due_at).isoformat(),
        deadline_at=None if job.deadline_at is None else
            _schedule_datetime("submission producer deadline_at", job.deadline_at).isoformat())
    _, canonical_utf8 = _submission_part_codec(
        dict(submission_part_request_revision=1, producer=producer), budget,
        maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    if type(canonical_utf8) is not bytes:
        raise TypeError("submission codec must return reserved exact UTF-8 bytes")
    budget.reserve(work=len(canonical_utf8))
    return hashlib.sha256(canonical_utf8).hexdigest()


def _submission_part_key_frontier(accession, document):
    document = inline_xbrl.safe_document_name(document)
    _receipt_text("submission part receipt key", "sec-submission-part:1:" + document, 512)
    _receipt_text("submission part outcome key", "derived:" + accession + ":" + document, 512)
    _receipt_text("submission part source key", accession + "/" + document, 1024)
    return document


def _submission_part_output_frontier(sizes, part):
    """Admit every prospective output size before any Put, without fabricating artifacts."""
    digest, size = part.content_sha256, len(part.payload)
    _capture_sha(digest)
    if not 0 < size <= _CAPTURE_BODY_BYTES:
        raise ValueError("submission output exceeds its ordinary body ceiling")
    previous = sizes.get(digest)
    if previous is not None and previous != size:
        raise ValueError("submission output conflicts with registered content size")
    sizes[digest] = size
    if len(sizes) > 64 or sum(sizes.values()) > _CAPTURE_TOTAL_BYTES:
        raise ValueError("submission prospective output union exceeds its ceiling")


def _submission_part_origin(admitted, part):
    return dict(revision=1, extraction_revision="SEC_NAMED_DOCUMENTS_1", fidelity="EXACT_CONTAINER_BYTES",
        type=part.document_type, sequence=part.sequence, description=part.description,
        separator=part.text_separator, enclosure=part.enclosure,
        raw_text=dict(start_byte=part.raw_text_start, end_byte=part.raw_text_end,
                      sha256=part.raw_text_sha256),
        content=dict(start_byte=part.content_start, end_byte=part.content_end,
                     sha256=part.content_sha256, byte_size=len(part.payload)),
        container=dict(acquisition_receipt=admitted.container_receipt.to_outcome(_raw_work_budget=admitted.budget),
                       artifact_sha256=admitted.container_artifact.content_sha256,
                       byte_size=admitted.container_artifact.byte_size,
                       source_url=admitted.container.observation.source_url),
        filing_acquisition_receipt=admitted.filing_receipt.to_outcome(_raw_work_budget=admitted.budget),
        extraction_job=dict(admitted.producer_association),
        request_identity_sha256=admitted.request_identity_sha256)


def _submission_part_verify_decoded_child(admitted, stored, part):
    """Compare authenticated stored origin with the exact verified container decoder result."""
    expected = _submission_part_origin(admitted, part)
    _, expected_bytes = _submission_part_codec(dict(origin=expected), admitted.budget,
                                               maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    _, actual_bytes = _submission_part_codec(dict(origin=stored.observation.canonical_metadata.get("derived_document")),
        admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    admitted.budget.equality(len(expected_bytes), len(actual_bytes))
    if expected_bytes != actual_bytes or stored.observation.artifact_sha256 != part.content_sha256:
        raise ValueError("registered submission child differs from exact decoded origin")


def _admit_submission_part_replay(owner, admitted, stored, item, metadata, part, context):
    key = "sec-submission-part:1:" + part.filename
    actual = _submission_part_receipt_lookup(owner, admitted.budget, producer_job_id=admitted.producer.job_id,
        receipt_key=key, request_identity_sha256=admitted.request_identity_sha256)
    if actual is None or actual.item != stored.item or actual.observation != stored.observation or actual.delivery != stored.delivery:
        raise ValueError("auxiliary acquisition differs from its actual receipt")
    role = metadata["role"]
    _submission_part_child_metadata(owner, admitted, actual, part.filename, role)
    _submission_part_verify_decoded_child(admitted, actual, part)
    identity = SourceItemIdentity(*(getattr(actual.item, name) for name in SourceItemIdentity.__dataclass_fields__))
    _, expected_metadata = _submission_part_codec(metadata, admitted.budget,
                                                  maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    _, actual_metadata = _submission_part_codec(actual.observation.canonical_metadata,
                                                admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    admitted.budget.equality(len(expected_metadata), len(actual_metadata))
    if (identity != item or actual.observation.state != "present"
            or actual.observation.artifact_sha256 != part.content_sha256
            or actual.observation.source_url != admitted.container.observation.source_url
            or actual.observation.etag is not None or actual.observation.last_modified is not None
            or actual.observation.source_published_at is not None or expected_metadata != actual_metadata):
        raise ValueError("auxiliary identity or exact derived origin differs")
    artifact = owner._artifacts.get(part.content_sha256)
    _admit_submission_part_artifact(owner, admitted, artifact, part)
    _submission_part_live_boundary(owner, context, admitted.budget)
    body = _read_retained_body(owner, admitted, part.content_sha256)
    admitted.budget.equality(len(body), len(part.payload))
    if body != part.payload:
        raise ValueError("registered auxiliary bytes differ from verified container part")

def _submission_part_origin_bounds(admitted, origin, digest, size):
    fields = {"revision", "extraction_revision", "fidelity", "type", "sequence", "description",
        "separator", "enclosure", "raw_text", "content", "container", "filing_acquisition_receipt",
        "extraction_job", "request_identity_sha256"}
    if type(origin) is not dict or set(origin) != fields:
        raise ValueError("submission child origin envelope differs")
    if (type(origin["revision"]) is not int or origin["revision"] != 1
            or origin["extraction_revision"] != "SEC_NAMED_DOCUMENTS_1"
            or origin["fidelity"] != "EXACT_CONTAINER_BYTES"
            or origin["request_identity_sha256"] != admitted.request_identity_sha256
            or type(origin["sequence"]) is not int or origin["sequence"] <= 0
            or origin["separator"] not in {"LF", "CRLF"} or origin["enclosure"] not in {"NONE", "XML", "XBRL"}):
        raise ValueError("submission child request or scalar origin differs")
    association = _submission_part_association(origin["extraction_job"])
    if association != _submission_part_association(dict(admitted.producer_association)):
        raise ValueError("submission child actual extraction association differs")
    for name in ("type", "description"):
        text = origin[name]
        if name == "description" and text is None:
            continue
        if type(text) is not str or len(text) > 4096 or "\0" in text or name == "type" and not text:
            raise ValueError("submission origin header text exceeds the parser frontier")
        admitted.budget.reserve(work=4 * len(text))
        if len(text.encode("utf-8")) > 4096:
            raise ValueError("submission origin header text exceeds the parser frontier")
    for name in ("raw_text", "content"):
        span = origin[name]
        fields = {"start_byte", "end_byte", "sha256"} | ({"byte_size"} if name == "content" else set())
        if type(span) is not dict or set(span) != fields:
            raise ValueError("submission child span has invalid fields")
        start, end = span["start_byte"], span["end_byte"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= admitted.container_artifact.byte_size:
            raise ValueError("submission child span lies outside its original container")
        _capture_sha(span["sha256"])
    raw, content = origin["raw_text"], origin["content"]
    if (not raw["start_byte"] <= content["start_byte"] <= content["end_byte"] <= raw["end_byte"]
            or type(content["byte_size"]) is not int
            or content["byte_size"] != content["end_byte"] - content["start_byte"]
            or content["byte_size"] != size or content["sha256"] != digest):
        raise ValueError("submission child content identity or size differs")
    roots = dict(container=dict(acquisition_receipt=admitted.container_receipt.to_outcome(_raw_work_budget=admitted.budget),
        artifact_sha256=admitted.container_artifact.content_sha256, byte_size=admitted.container_artifact.byte_size,
        source_url=admitted.container.observation.source_url), filing_acquisition_receipt=admitted.filing_receipt.to_outcome(_raw_work_budget=admitted.budget))
    for name, value in roots.items():
        _, actual = _submission_part_codec(dict(value=origin[name]), admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
        _, expected = _submission_part_codec(dict(value=value), admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
        admitted.budget.equality(len(actual), len(expected))
        if actual != expected:
            raise ValueError("submission child original container/filing association differs")


def _submission_part_validate_child(admitted, metadata, document, role, digest, size):
    """Share exact derived metadata/origin/envelope admission before writes and on reuse."""
    _capture_sha(digest)
    if type(size) is not int or not 0 < size <= _CAPTURE_BODY_BYTES:
        raise ValueError("submission child ordinary size differs")
    origin = metadata.get("derived_document") if type(metadata) is dict else None
    _submission_part_origin_bounds(admitted, origin, digest, size)
    resource = _SecResource(role, "resource", admitted.accession + "/" + document,
                            admitted.container.observation.source_url, True)
    expected = _resource_metadata(resource=resource, role=role, accession=admitted.accession,
        cik=admitted.cik, form=admitted.form, filename=admitted.filename, parent=admitted.parent)
    expected.update(document=document, derived_document=origin)
    detached, actual_bytes = _submission_part_codec(metadata, admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    _, expected_bytes = _submission_part_codec(expected, admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    admitted.budget.equality(len(actual_bytes), len(expected_bytes))
    if actual_bytes != expected_bytes:
        raise ValueError("submission child canonical source metadata differs")
    url = admitted.container.observation.source_url
    admitted.budget.reserve(work=4 * len(url))
    if _canonical_url(url) != url:
        raise ValueError("submission child source URL is not the original canonical container URL")
    envelope = dict(state="present", metadata=detached, artifact_sha256=digest, source_url=url,
                    etag=None, last_modified=None, source_published_at=None)
    _submission_part_json_size(envelope, admitted.budget, _SUBMISSION_PART_REQUEST_BYTES)
    return detached, len(actual_bytes)

def _submission_part_live_boundary(owner, context, budget):
    """Check this actual lease and deadline; release the row lock before any I/O."""
    with owner._jobs.engine.begin() as connection:
        row = owner._jobs._lock_live_lease(connection, context.lease, owner._clock, _raw_work_budget=budget)
        current = _schedule_datetime("submission boundary clock", owner._clock())
        if _schedule_datetime("submission lease expiry", row["lease_expires_at"]) <= current:
            raise LeaseLost("submission parts lease expired at the boundary")
        deadline = row["deadline_at"]
        if deadline is not None and _schedule_datetime("submission deadline", deadline) <= current:
            raise ValueError("submission parts producer deadline reached")

@dataclass(frozen=True)
class _RetainedDocuments:
    primary: str
    auxiliaries: tuple[tuple[str, str], ...]
    projections: tuple[tuple[str, tuple[str, ...]], ...]


def _required_retained_documents(inventory, submissions, *, parent, accession, cik, form):
    """Select current domestic inline documents from verified registered roots."""
    if form not in {"10-K", "10-K/A"} or not _same_cik(submissions.get("cik"), cik):
        raise ValueError("retained submission profile requires current domestic annual roots")
    annual = _recent_filing_metadata(submissions, accession)
    if annual is None:
        raise SecResourcePending("retained recent submissions do not name the filing")
    if (annual["form"] != form
            or annual["filed"] != parent.observation.canonical_metadata.get("filing_date")):
        raise ValueError("retained filing metadata differs from its original parent")
    names = _inventory_names(inventory, accession=accession, cik=cik)
    documents = sync._inline_documents(dict(inventory), annual["document"])
    projections = {}
    for role, document in documents.items():
        if document != annual["document"]:
            projections.setdefault(document, []).append(role)
    for report in sorted(name for name in names if re.fullmatch(r"R[1-3]\.htm", name, re.I)):
        projections.setdefault(report, []).append("cover_" + report[:-4].casefold())
    selected = tuple((document, roles[0]) for document, roles in sorted(projections.items()))
    if not selected or len(selected) > sec_submission.MAX_REQUESTS:
        raise ValueError("retained auxiliary count exceeds the decoder profile")
    return _RetainedDocuments(annual["document"], selected,
        tuple((document, tuple(roles)) for document, roles in sorted(projections.items())))


@dataclass(frozen=True)
class _SubmissionPartMetadata:
    producer: Any
    producer_association: Mapping[str, Any]
    producer_sha256: str
    request_identity_sha256: str
    resource: Any
    resource_association: Mapping[str, Any]
    resource_stamp: bytes
    execution: bytes
    accession: str
    cik: str
    form: str
    filename: str
    parent: Any
    filing_receipt: SourceAcquisitionReceipt
    root_receipts: tuple[tuple[str, SourceAcquisitionReceipt], ...]
    sources: tuple[tuple[str, Any, bytes], ...]
    container: Any
    container_receipt: SourceAcquisitionReceipt
    container_artifact: Any
    auxiliaries: tuple[tuple[str, str], ...]
    receipts: Mapping[str, Any]
    budget: Any
    artifacts: Mapping[str, Any]


@dataclass(frozen=True)
class _SubmissionPartAdmission(_SubmissionPartMetadata):
    primary_document: str
    projections: tuple[tuple[str, tuple[str, ...]], ...]
    bodies: dict[str, bytes]
    maxima: Mapping[str, int]
    _maxima: dict[str, int]


def _submission_part_association(value):
    if type(value) is not dict or set(value) != {"job_id", "occurrence_id", "schedule_key", "scheduled_for"}:
        raise ValueError("submission producer association has invalid fields")
    for name in ("job_id", "occurrence_id"):
        _capture_id(name, value[name])
    key = _bounded_schedule_text("submission schedule_key", value["schedule_key"], 1024)
    text = _bounded_schedule_text("submission scheduled_for", value["scheduled_for"], 64)
    instant = _schedule_datetime("submission scheduled_for", datetime.fromisoformat(text))
    if instant.isoformat() != text:
        raise ValueError("submission association requires canonical UTC time")
    return dict(job_id=value["job_id"], occurrence_id=value["occurrence_id"],
                schedule_key=key, scheduled_for=instant)


def _submission_part_execution(budget):
    token = _SUBMISSION_PART_FINGERPRINT_BUDGET.set(budget)
    pair_token = _SUBMISSION_PART_FINGERPRINT_PAIR.set((budget, {}))
    try:
        from .captured_assembly import engine_identity
        from .sec_inventory_plan import producer_build_sha256
        value = dict(engine_revision=store.ENGINE_VERSION,
                     engine_identity_sha256=engine_identity(),
                     producer_build_sha256=producer_build_sha256())
    finally:
        _SUBMISSION_PART_FINGERPRINT_PAIR.reset(pair_token)
        _SUBMISSION_PART_FINGERPRINT_BUDGET.reset(token)
    return _submission_part_codec(value, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)[1]


def _submission_part_source_pin(stored, budget):
    item, observation, delivery = stored.item, stored.observation, stored.delivery
    if delivery is None:
        raise ValueError("submission source lacks its original full delivery")
    metadata_size, _ = _submission_part_json_size(observation.canonical_metadata, budget, 65536)
    budget.reserve(work=metadata_size + 256)
    value = dict(item=asdict(item), observation=asdict(observation), delivery=_delivery_values(delivery))
    value["item"]["created_at"] = item.created_at.isoformat()
    for name in ("source_published_at", "detected_at"):
        instant = getattr(observation, name)
        value["observation"][name] = None if instant is None else instant.isoformat()
    return _submission_part_codec(value, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)[1]


def _submission_part_receipt_lookup(owner, budget, *, producer_job_id, receipt_key, request_identity_sha256):
    """Preflight a ledger hint; only the existing getter authenticates source/full delivery."""
    _capture_id("submission receipt producer", producer_job_id)
    _receipt_text("submission receipt key", receipt_key, 512)
    _capture_sha(request_identity_sha256)
    with owner._jobs.engine.connect() as connection:
        hint = owner._jobs._item_outcome_on_connection(connection, producer_job_id,
                                                      _SOURCE_RECEIPT_PREFIX + receipt_key, _raw_work_budget=budget)
    if hint is None:
        # Absence is not source admission. Any later record still uses the fenced source owner.
        return None
    raw_value = hint["outcome"]
    _reserve_submission_part_receipt(budget, phase="codec", raw_value=raw_value)
    receipt = SourceAcquisitionReceipt.from_outcome(raw_value)
    if (receipt.producer_job_id != producer_job_id or receipt.receipt_key != receipt_key
            or receipt.request_identity_sha256 != request_identity_sha256):
        raise ValueError("submission receipt hint belongs to another immutable request")
    preflight = owner._observations.by_sha256(receipt.observation_sha256, _raw_work_budget=budget)
    _, metadata = _submission_part_codec(preflight.observation.canonical_metadata, budget,
                                        maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    _reserve_submission_part_receipt(budget, phase="lookup", metadata_bytes=len(metadata),
                                    observation=preflight.observation)
    observation = preflight.observation
    if observation_sha256(state=observation.state, metadata=observation.canonical_metadata,
            artifact_sha256=observation.artifact_sha256, source_url=observation.source_url,
            etag=observation.etag, last_modified=observation.last_modified,
            source_published_at=observation.source_published_at) != receipt.observation_sha256:
        raise ValueError("submission receipt preflight source hash differs")
    stored = owner._observations.acquisition_receipt(producer_job_id=producer_job_id,
        receipt_key=receipt_key, request_identity_sha256=request_identity_sha256, _raw_work_budget=budget)
    if stored is None:
        raise ValueError("submission receipt disappeared after its non-authoritative preflight")
    _, actual_metadata = _submission_part_codec(stored.observation.canonical_metadata, budget,
                                               maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    budget.equality(len(metadata), len(actual_metadata))
    if stored.item != preflight.item or metadata != actual_metadata:
        raise ValueError("submission receipt source differs from bounded preflight")
    return stored


def _submission_part_actual_receipt(owner, receipt, budget):
    stored = _submission_part_receipt_lookup(owner, budget, producer_job_id=receipt.producer_job_id,
        receipt_key=receipt.receipt_key, request_identity_sha256=receipt.request_identity_sha256)
    if stored is None:
        raise ValueError("submission acquisition receipt is not registered")
    item = SourceItemIdentity(*(getattr(stored.item, name) for name in SourceItemIdentity.__dataclass_fields__))
    actual = SourceAcquisitionReceipt(receipt.producer_job_id, receipt.request_identity_sha256,
        receipt.receipt_key, item, stored.observation.observation_sha256, stored.delivery)
    _, supplied = _submission_part_codec(receipt.to_outcome(_raw_work_budget=budget), budget, maximum_bytes=8192)
    _, registered = _submission_part_codec(actual.to_outcome(_raw_work_budget=budget), budget, maximum_bytes=8192)
    budget.equality(len(supplied), len(registered))
    if supplied != registered:
        raise ValueError("submission receipt differs from original item/observation/full delivery")
    return stored


def _submission_part_admit_artifact(owner, artifacts, digest):
    _capture_sha(digest)
    artifact = owner._artifacts.get(digest)
    if (artifact.content_sha256 != digest or artifact.object_key != "raw/sha256/" + digest
            or type(artifact.byte_size) is not int or not 0 <= artifact.byte_size <= _CAPTURE_BODY_BYTES):
        raise ValueError("submission artifact identity or ordinary body ceiling differs")
    _bounded_schedule_text("registered submission media type", artifact.media_type, 256)
    for name in ("created_at", "verified_at"):
        _schedule_datetime("submission artifact " + name, getattr(artifact, name))
    previous = artifacts.get(digest)
    if previous is not None and previous != artifact:
        raise ValueError("registered submission artifact metadata changed")
    if previous is None:
        if len(artifacts) >= 64 or sum(record.byte_size for record in artifacts.values()) + artifact.byte_size > _CAPTURE_TOTAL_BYTES:
            raise ValueError("submission registered artifact union exceeds its ceiling")
        artifacts[digest] = artifact
    return artifact


def _submission_part_child_metadata(owner, admitted, stored, document, role):
    observation = stored.observation
    expected_item = SourceItemIdentity("SEC", "resource", admitted.accession + "/" + document,
        admitted.cik, admitted.parent.item.source_item_id)
    actual_item = SourceItemIdentity(*(getattr(stored.item, name) for name in SourceItemIdentity.__dataclass_fields__))
    if (actual_item != expected_item or observation.state != "present"
            or observation.source_url != admitted.container.observation.source_url
            or observation.etag is not None or observation.last_modified is not None
            or observation.source_published_at is not None):
        raise ValueError("registered submission child identity or source envelope differs")
    artifact = _submission_part_admit_artifact(owner, admitted.artifacts, observation.artifact_sha256)
    _submission_part_validate_child(admitted, observation.canonical_metadata, document, role,
                                    artifact.content_sha256, artifact.byte_size)
    return artifact

def _admit_submission_part_source_metadata(owner, association, budget):
    """Authenticate the actual extraction request and source origins without reading bodies."""
    engine = owner._jobs.engine
    if owner._observations.engine is not engine or owner._artifacts.engine is not engine:
        raise ValueError("submission source admission requires same-engine retained owners")
    association = _submission_part_association(dict(association))
    producer = owner._jobs.scheduled_job(**association, _raw_work_budget=budget)
    if producer.kind != "sec-submission-parts-v1":
        raise ValueError("submission origin requires its actual extraction producer kind")
    parameters, _ = _submission_part_codec(producer.parameters, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    if set(parameters) != {"request_revision", "operation_key", "operation_at", "execution", "resource",
                           "acquisitions", "documents", "extraction_revision"}:
        raise ValueError("submission extraction request has invalid fields")
    if (type(parameters["request_revision"]) is not int or parameters["request_revision"] != 1
            or parameters["extraction_revision"] != "SEC_NAMED_DOCUMENTS_1"):
        raise ValueError("submission extraction revision differs")
    declared_association = _submission_part_association(dict(job_id=producer.job_id, occurrence_id=producer.occurrence_id,
        schedule_key=parameters["operation_key"], scheduled_for=parameters["operation_at"]))
    if declared_association != association:
        raise ValueError("submission extraction parameters identify another scheduled association")
    request = _submission_part_request_identity(producer, budget)
    execution_value = parameters["execution"]
    if type(execution_value) is not dict or set(execution_value) != {
            "engine_revision", "engine_identity_sha256", "producer_build_sha256"}:
        raise ValueError("submission historical execution envelope differs")
    _capture_id("submission historical engine revision", execution_value["engine_revision"])
    _capture_sha(execution_value["engine_identity_sha256"])
    _capture_sha(execution_value["producer_build_sha256"])
    _, execution = _submission_part_codec(parameters["execution"], budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    supplied_resource = parameters["resource"]
    if type(supplied_resource) is not dict or set(supplied_resource) != {"job_id", "occurrence_id", "schedule_key", "scheduled_for", "job_stamp_sha256"}:
        raise ValueError("submission original resource association has invalid fields")
    resource_association = _submission_part_association({name: supplied_resource[name] for name in (
        "job_id", "occurrence_id", "schedule_key", "scheduled_for")})
    _capture_sha(supplied_resource["job_stamp_sha256"])
    resource = owner._jobs.scheduled_job(**resource_association, _raw_work_budget=budget)
    if resource.kind not in _RESOURCE_KINDS or resource.status != "succeeded":
        raise ValueError("submission extraction requires its succeeded original resource")
    _submission_part_codec(resource.parameters, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    if resource.checkpoint is not None:
        _submission_part_codec(resource.checkpoint, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    # Charge the existing stamp/request codecs before calling them; no second ledger.
    _reserve_submission_part_original_request(budget, resource, phase="stamp")
    _, stamp = _submission_part_codec(_capture_job_stamp(resource), budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    budget.reserve(work=len(stamp))
    if hashlib.sha256(stamp).hexdigest() != supplied_resource["job_stamp_sha256"]:
        raise ValueError("submission original resource stamp differs")
    accession, cik, form, filename, observation_id = _resource_parameters(resource.parameters)
    checkpoint = _resource_checkpoint(resource.checkpoint, accession=accession, cik=cik, observation_id=observation_id)
    if checkpoint is None or type(checkpoint.get("capture_revision")) is not int or checkpoint["capture_revision"] != 2:
        raise ValueError("submission resource lacks original capture2 deliveries")
    values = parameters["acquisitions"]
    if type(values) is not list or len(values) != 4:
        raise ValueError("submission extraction requires exactly four original acquisitions")
    receipts = []
    for value in values:
        _reserve_submission_part_receipt(budget, phase="codec", raw_value=value)
        receipts.append(SourceAcquisitionReceipt.from_outcome(value))
    order = tuple((r.delivery.source_item_id, r.delivery.source_observation_id, r.delivery.occurrence_id) for r in receipts)
    if order != tuple(sorted(set(order))):
        raise ValueError("submission acquisitions must be sorted and unique")
    sources = {}
    for receipt in receipts:
        stored = _submission_part_actual_receipt(owner, receipt, budget)
        role = "filing" if stored.item.item_kind == "filing" else stored.observation.canonical_metadata.get("role")
        if role not in {"filing", "complete_submission", "accession_inventory", "submissions"} or role in sources:
            raise ValueError("submission acquisitions have an unexpected or duplicate role")
        sources[role] = (stored, receipt)
    if set(sources) != {"filing", "complete_submission", "accession_inventory", "submissions"}:
        raise ValueError("submission original acquisition roles differ")
    parent, filing_receipt = sources["filing"]
    _validate_parent_filing(parent, accession=accession, cik=cik, form=form, filename=filename, observation_id=observation_id)
    artifacts = {}
    _submission_part_source_pin(parent, budget)
    _submission_part_admit_artifact(owner, artifacts, parent.observation.artifact_sha256)
    descriptors = _root_resources(accession=accession, cik=cik, filename=filename)
    original_job = dict(job_id=resource.job_id, occurrence_id=resource.occurrence_id, kind=resource.kind,
        parameters=resource.parameters, priority=resource.priority, max_attempts=resource.max_attempts,
        due_at=_schedule_datetime("original resource due_at", resource.due_at).isoformat(),
        deadline_at=None if resource.deadline_at is None else
            _schedule_datetime("original resource deadline_at", resource.deadline_at).isoformat())
    original_request = _partial_resource_request(original_job, parent, descriptors, cik,
                                                parent.observation.canonical_metadata.get("filing_date"), budget=budget)
    for descriptor in descriptors:
        if descriptor.role == "company_facts":
            continue
        stored, receipt = sources[descriptor.role]
        retained = _retained_root_source(owner._observations, checkpoint["roots"][descriptor.role],
            role=descriptor.role, parent=parent, accession=accession, cik=cik, budget=budget)
        expected_item = _resource_identity(descriptor, cik=cik, parent=parent)
        actual_item = SourceItemIdentity(*(getattr(stored.item, name) for name in SourceItemIdentity.__dataclass_fields__))
        expected_metadata = _resource_metadata(resource=descriptor, role=descriptor.role, accession=accession,
            cik=cik, form=form, filename=filename, parent=parent)
        if stored.item != retained.item or stored.observation != retained.observation or actual_item != expected_item:
            raise ValueError("submission original resource/source association differs")
        metadata, _ = _submission_part_codec(stored.observation.canonical_metadata, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
        for name, value in expected_metadata.items():
            if metadata.get(name) != value or type(metadata.get(name)) is not type(value):
                raise ValueError("submission original parent/source metadata differs")
        delivery = _retained_root_delivery(owner._observations, stored, checkpoint["root_deliveries"][descriptor.role],
                                            compare_instants=True, strict_revision=True)
        phase = metadata.get("partial_acquisition")
        _partial_phase(phase)
        if (receipt.request_identity_sha256 != original_request
                or receipt.receipt_key != "sec-resource-partial:1:ready:" + descriptor.role
                or phase["phase"] != "READY" or phase["failure"] is not None
                or phase["requested_url"] != descriptor.url or not phase["http_headers_captured"]
                or stored.observation.source_url != _canonical_url(phase["http_response_url"])
                or stored.observation.etag != _optional_text("etag", phase["http_etag"])
                or stored.observation.last_modified != _optional_text("last_modified", phase["http_last_modified"])
                or stored.observation.source_published_at is not None
                or delivery.item_key != "root:" + descriptor.role
                or delivery != receipt.delivery or receipt.producer_job_id != resource.job_id):
            raise ValueError("submission root acquisition differs from original resource full delivery")
        _submission_part_admit_artifact(owner, artifacts, stored.observation.artifact_sha256)
    documents = parameters["documents"]
    if type(documents) is not list or not 0 < len(documents) <= sec_submission.MAX_REQUESTS:
        raise ValueError("submission requested auxiliary count differs")
    selected = []
    for value in documents:
        if type(value) is not dict or set(value) != {"document", "source_role"}:
            raise ValueError("submission auxiliary descriptor has invalid fields")
        selected.append((_submission_part_key_frontier(accession, value["document"]),
                         _bounded_schedule_text("submission source role", value["source_role"], 256)))
    if tuple(selected) != tuple(sorted(set(selected))) or len({name for name, _ in selected}) != len(selected):
        raise ValueError("submission auxiliary descriptors must be sorted and unique")
    association_text = dict(association, scheduled_for=association["scheduled_for"].isoformat())
    admitted = _SubmissionPartMetadata(producer, MappingProxyType(association_text), request, request,
        resource, MappingProxyType(resource_association), stamp, execution, accession, cik, form, filename,
        parent, filing_receipt, tuple((role, receipt) for role, (_, receipt) in sorted(sources.items()) if role != "filing"),
        tuple((role, stored, _submission_part_source_pin(stored, budget)) for role, (stored, _) in sorted(sources.items())),
        sources["complete_submission"][0], sources["complete_submission"][1],
        artifacts[sources["complete_submission"][0].observation.artifact_sha256], tuple(selected),
        MappingProxyType({}), budget, artifacts)
    # Authenticate all existing children before reading either root or container.
    existing = {}
    for document, role in selected:
        child = _submission_part_receipt_lookup(owner, budget, producer_job_id=producer.job_id,
            receipt_key="sec-submission-part:1:" + document, request_identity_sha256=request)
        if child is not None:
            _submission_part_child_metadata(owner, admitted, child, document, role)
            existing[document] = child
    return replace(admitted, receipts=MappingProxyType(existing), artifacts=MappingProxyType(artifacts))


def _admit_submission_part_request(owner, context):
    engine = owner._jobs.engine
    if (not isinstance(context, JobContext) or context._repository.engine is not engine
            or owner._observations.engine is not engine or owner._artifacts.engine is not engine):
        raise ValueError("submission extraction requires same-engine source/artifact/job/context owners")
    budget = _AdmissionBudget()
    parameters, _ = _submission_part_codec(context.job.parameters, budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    current_execution = _submission_part_execution(budget)
    _, claimed_execution = _submission_part_codec(parameters.get("execution"), budget,
                                                 maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    budget.equality(len(current_execution), len(claimed_execution))
    if current_execution != claimed_execution:
        raise ValueError("submission extraction executing source pins differ")
    association = dict(job_id=context.job.job_id, occurrence_id=context.job.occurrence_id,
        schedule_key=parameters.get("operation_key"), scheduled_for=parameters.get("operation_at"))
    metadata = _admit_submission_part_source_metadata(owner, association, budget)
    if metadata.producer_sha256 != _submission_part_request_identity(context.job, budget):
        raise ValueError("submission context differs from immutable scheduled producer")
    maximums = {digest: _CAPTURE_BODY_BYTES for digest in metadata.artifacts}
    values = {**vars(metadata), "artifacts": dict(metadata.artifacts)}
    admitted = _SubmissionPartAdmission(**values, primary_document="", projections=(), bodies={},
        maxima=MappingProxyType(maximums), _maxima=maximums)
    _recheck_submission_part_request(owner, admitted, context)
    sources = {role: stored for role, stored, _ in admitted.sources}
    _submission_part_live_boundary(owner, context, admitted.budget)
    inventory_body = _read_retained_body(owner, admitted, sources["accession_inventory"].observation.artifact_sha256)
    _submission_part_live_boundary(owner, context, admitted.budget)
    submissions_body = _read_retained_body(owner, admitted, sources["submissions"].observation.artifact_sha256)
    budget.json_decode(len(inventory_body))
    inventory = _strict_json_object(inventory_body, "retained SEC accession inventory")
    budget.json_decode(len(submissions_body))
    submissions = _strict_json_object(submissions_body, "retained SEC submissions")
    required = _required_retained_documents(inventory, submissions, parent=admitted.parent,
        accession=admitted.accession, cik=admitted.cik, form=admitted.form)
    if admitted.auxiliaries != required.auxiliaries or required.primary in {name for name, _ in admitted.auxiliaries}:
        raise ValueError("submission auxiliary request differs from the owning retained graph")
    admitted = replace(admitted, primary_document=required.primary, projections=required.projections)
    return admitted


def _recheck_submission_part_source_metadata(owner, admitted):
    association = _submission_part_association(dict(admitted.producer_association))
    producer = owner._jobs.scheduled_job(**association, _raw_work_budget=admitted.budget)
    if _submission_part_request_identity(producer, admitted.budget) != admitted.producer_sha256:
        raise ValueError("submission producer immutable request changed")
    resource = owner._jobs.scheduled_job(**admitted.resource_association, _raw_work_budget=admitted.budget)
    _submission_part_codec(resource.parameters, admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    if resource.checkpoint is not None:
        _submission_part_codec(resource.checkpoint, admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    _reserve_submission_part_original_request(admitted.budget, resource, phase="stamp")
    _, stamp = _submission_part_codec(_capture_job_stamp(resource), admitted.budget, maximum_bytes=_SUBMISSION_PART_REQUEST_BYTES)
    admitted.budget.equality(len(stamp), len(admitted.resource_stamp))
    if stamp != admitted.resource_stamp:
        raise ValueError("submission original resource changed")
    receipts = dict(admitted.root_receipts, filing=admitted.filing_receipt)
    for role, _, pin in admitted.sources:
        actual = _submission_part_actual_receipt(owner, receipts[role], admitted.budget)
        current_pin = _submission_part_source_pin(actual, admitted.budget)
        admitted.budget.equality(len(pin), len(current_pin))
        if current_pin != pin:
            raise ValueError("submission original source/delivery changed")
    for digest, expected in tuple(admitted.artifacts.items()):
        if owner._artifacts.get(digest) != expected:
            raise ValueError("submission registered artifact metadata changed before write")


def _recheck_submission_part_request(owner, admitted, context):
    if admitted.producer.job_id != context.lease.job_id:
        raise ValueError("submission writer lease belongs to another actual producer")
    _recheck_submission_part_source_metadata(owner, admitted)
    execution = _submission_part_execution(admitted.budget)
    admitted.budget.equality(len(execution), len(admitted.execution))
    if execution != admitted.execution:
        raise ValueError("submission executing source pins changed")
    parent = owner._observations.latest_sec_filing(admitted.parent.observation.source_observation_id, _raw_work_budget=admitted.budget)
    if parent.item != admitted.parent.item or parent.observation != admitted.parent.observation:
        raise ValueError("submission current parent changed")
    _submission_part_live_boundary(owner, context, admitted.budget)


def _admit_submission_part_artifact(owner, admitted, artifact, part):
    actual = _submission_part_admit_artifact(owner, admitted.artifacts, part.content_sha256)
    if actual != artifact or actual.byte_size != len(part.payload):
        raise ValueError("submission stored/replayed artifact differs from exact parser part")
    admitted._maxima[actual.content_sha256] = _CAPTURE_BODY_BYTES
    # Preserve actual first-registration MIME. A new SHA is now eligible for a reader cache miss.
    return actual

def _partial_resource_job(context):
    job, lease = context.job, context.lease
    for name, value in (("job", job.job_id), ("occurrence", job.occurrence_id),
                        ("generation", job.ownership_generation), ("lease job", lease.job_id),
                        ("lease generation", lease.generation)):
        _capture_id(name, value)
    if job.job_id != lease.job_id or job.ownership_generation != lease.generation:
        raise ValueError("partial acquisition context differs from its actual lease")
    if type(job.kind) is not str or len(job.kind) > 256:
        raise ValueError("partial acquisition kind must be bounded exact text")
    _bounded_schedule_text("resource kind", job.kind, 256)
    if job.kind not in _RESOURCE_KINDS:
        raise ValueError("partial acquisition requires a SEC resource job")
    if (type(job.priority) is not int or not -2**31 <= job.priority < 2**31
            or type(job.max_attempts) is not int or not 0 < job.max_attempts < 2**31):
        raise ValueError("partial acquisition schedule scalars are invalid")
    parameters, _ = _bounded_schedule_parameters(job.parameters)
    _capture_id("filing observation", parameters.get("observation_id"))
    if job.checkpoint is not None:
        _bounded_schedule_parameters(job.checkpoint)
    return dict(job_id=job.job_id, occurrence_id=job.occurrence_id, kind=job.kind,
        parameters=parameters, priority=job.priority, max_attempts=job.max_attempts,
        due_at=_schedule_datetime("resource due_at", job.due_at).isoformat(),
        deadline_at=None if job.deadline_at is None else
            _schedule_datetime("resource deadline", job.deadline_at).isoformat())


def _partial_root_maximum(resource):
    return _CAPTURE_FACTS_BYTES if resource.role == "company_facts" else _CAPTURE_BODY_BYTES


def _partial_resource_request(job, parent, resources, cik, filing_date, *, budget=None):
    _capture_id("parent source item", parent.item.source_item_id)
    _capture_id("parent observation", parent.observation.source_observation_id)
    _capture_sha(parent.observation.observation_sha256)
    _capture_text(filing_date)
    descriptors = []
    for resource in resources:
        identity = _resource_identity(resource, cik=cik, parent=parent)
        _bounded_schedule_text("resource source key", identity.source_key, 1024)
        _capture_text(resource.url)
        descriptors.append(dict(role=resource.role, item=asdict(identity), url=resource.url,
                                parented=resource.parented, maximum_bytes=_partial_root_maximum(resource)))
    request = dict(partial_acquisition_revision=1, capture_revision=2, job=job,
        parent=dict(source_item_id=parent.item.source_item_id,
            source_observation_id=parent.observation.source_observation_id,
            observation_sha256=parent.observation.observation_sha256, filing_date=filing_date),
        resources=descriptors, limits=dict(body_bytes=_CAPTURE_BODY_BYTES,
            facts_bytes=_CAPTURE_FACTS_BYTES, total_bytes=_CAPTURE_TOTAL_BYTES,
            reference_bytes=_CAPTURE_REFERENCE_BYTES, phase_bytes=65536, failure_characters=2000))
    if budget is not None:
        _reserve_submission_part_original_request(budget, job, phase='partial_request', request=request)
    _, canonical = _bounded_schedule_parameters(request)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _partial_phase(phase):
    fields = {"revision", "phase", "requested_url", "http_headers_captured",
              "http_response_url", "http_media_type", "http_etag", "http_last_modified", "failure"}
    if (type(phase) is not dict or set(phase) != fields or type(phase["revision"]) is not int
            or phase["revision"] != 1 or type(phase["phase"]) is not str
            or phase["phase"] not in {"READY", "FAILED_PENDING"}
            or type(phase["http_headers_captured"]) is not bool):
        raise ValueError("partial acquisition has invalid phase fields")
    for name in ("requested_url", "http_response_url", "http_media_type", "http_etag", "http_last_modified"):
        _capture_text(phase[name])
    if not phase["requested_url"]:
        raise ValueError("partial acquisition requires requested URL")
    failure = phase["failure"]
    if phase["phase"] == "READY":
        if failure is not None or phase["http_headers_captured"] is not True or not phase["http_response_url"]:
            raise ValueError("ready acquisition lacks its actual response")
    else:
        if (type(failure) is not dict or set(failure) != {"kind", "message", "raised_message"}
                or type(failure["kind"]) is not str
                or failure["kind"] not in {"NoXbrlDataError", "SecResourcePending"}):
            raise ValueError("partial acquisition lacks its original typed failure")
        for name in ("message", "raised_message"):
            value = failure[name]
            if type(value) is not str or len(value) > 2000 or not value.strip():
                raise ValueError("partial acquisition failure message exceeds2000 characters")
            _capture_text(value)
        absent = failure["kind"] == "NoXbrlDataError"
        if absent:
            if phase["http_headers_captured"] is not False or any(phase[name] is not None for name in
                    ("http_response_url", "http_media_type", "http_etag", "http_last_modified")):
                raise ValueError("not-found acquisition cannot claim response headers or final URL")
        elif (phase["http_headers_captured"] is not True or not phase["http_response_url"]
                or failure["raised_message"] != failure["message"]):
            raise ValueError("structural pending must preserve response and message")
    size = 0
    for part in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(phase):
        size += len(part.encode("utf-8"))
        if size > 65536:
            raise ValueError("partial acquisition phase exceeds64KiB")


def _partial_root_admission(stored, identity, metadata, resource, request, key, context):
    item, observation, delivery = stored.item, stored.observation, stored.delivery
    phase = observation.canonical_metadata.get("partial_acquisition")
    _partial_phase(phase)
    failure = phase["failure"]
    absent = failure is not None and failure["kind"] == "NoXbrlDataError"
    expected = {**metadata, "partial_acquisition": phase}
    if absent:
        expected["reason"] = "not_found"
        if failure["raised_message"] != f"SEC root resource is pending: {resource.role}":
            raise ValueError("not-found acquisition changed its pending disposition")
    else:
        expected.update(capture_revision=2, capture_fidelity="RAW_HTTP_BYTES")
    actual_identity = SourceItemIdentity(*(getattr(item, name) for name in SourceItemIdentity.__dataclass_fields__))
    if item.parent_source_item_id is not None:
        _capture_id("resource parent item", item.parent_source_item_id)
    _bounded_schedule_parameters(observation.canonical_metadata)
    if (actual_identity != identity or _canonical_json(observation.canonical_metadata) != _canonical_json(expected)
            or phase["requested_url"] != resource.url
            or phase["phase"] != ("READY" if ":ready:" in key else "FAILED_PENDING")
            or observation.state != ("pending" if absent else "present")
            or (observation.artifact_sha256 is None) != absent
            or observation.source_url != _canonical_url(resource.url if absent else phase["http_response_url"])
            or observation.etag != _optional_text("etag", phase["http_etag"])
            or observation.last_modified != _optional_text("last_modified", phase["http_last_modified"])
            or observation.source_published_at is not None or delivery is None):
        raise ValueError("partial acquisition differs from its original resource request")
    for name, value in (("source item", item.source_item_id), ("source observation", observation.source_observation_id)):
        _capture_id(name, value)
    if (delivery.job_id != context.job.job_id or delivery.source_item_id != item.source_item_id
            or delivery.source_observation_id != observation.source_observation_id
            or delivery.item_key != f"root:{resource.role}"
            or delivery.ownership_generation > context.lease.generation
            or phase["phase"] == "FAILED_PENDING" and delivery.ownership_generation != context.lease.generation):
        raise ValueError("partial acquisition delivery differs from its original producer")
    encoded = SourceAcquisitionReceipt(context.job.job_id, request, key, identity,
        observation.observation_sha256, delivery).to_outcome()["delivery"]
    return phase, encoded


def _partial_root_bytes(raw, maximum, budget):
    if type(raw) is not bytes or len(raw) > maximum:
        raise ValueError("partial SEC body exceeds its role ceiling")
    budget["bytes"] += len(raw)
    digest = hashlib.sha256(raw).hexdigest()
    budget["artifacts"].setdefault(digest, len(raw))
    if budget["bytes"] > 56 * 1024 * 1024 or sum(budget["artifacts"].values()) > _CAPTURE_TOTAL_BYTES:
        raise ValueError("partial SEC bodies exceed aggregate ceiling")


def _partial_root_pending(context, resource, stored, failure):
    context.record_item_outcome(item_key=f"root:{resource.role}", status="failed",
        outcome=dict(state="pending", source_item_id=stored.item.source_item_id,
            source_observation_id=stored.observation.source_observation_id,
            observation_sha256=stored.observation.observation_sha256), error_summary=failure["raised_message"])
    error = SecResourcePending(failure["raised_message"])
    if failure["kind"] == "NoXbrlDataError":
        raise error from NoXbrlDataError(failure["message"])
    raise error


def _resource_parameters(
    parameters: Mapping[str, Any],
) -> tuple[str, str, str, str, int]:
    required = {"accession", "cik", "form", "filename", "observation_id"}
    if not isinstance(parameters, Mapping) or set(parameters) != required:
        raise ValueError(
            "SEC resource fetch requires only accession, cik, form, filename, "
            "and observation_id"
        )
    accession, cik, form, filename = _resource_identity_parameters(parameters)
    observation_id = parameters["observation_id"]
    if (
        isinstance(observation_id, bool)
        or not isinstance(observation_id, int)
        or observation_id <= 0
    ):
        raise ValueError("observation_id must be a positive integer")
    return accession, cik, form, filename, observation_id


def _candidate_parameters(
    parameters: Mapping[str, Any],
) -> tuple[str, str, str, str, int, dict[str, str]]:
    expected = {
        "accession",
        "cik",
        "form",
        "filename",
        "observation_id",
        "roots",
    }
    captured_fields = {"capture_revision", "root_deliveries"}
    planned_fields = {"primary_input_revision", "primary_input_plan_sha256"}
    if not isinstance(parameters, Mapping) or set(parameters) not in (
            expected, expected | captured_fields, expected | captured_fields | planned_fields,
            expected | captured_fields | planned_fields | {'retained_stage'}):
        raise ValueError(
            "SEC candidate stage requires filing parameters and exactly four roots"
        )
    accession, cik, form, filename, observation_id = _resource_parameters(
        {key: parameters[key] for key in (
            "accession", "cik", "form", "filename", "observation_id"
        )}
    )
    roots = parameters["roots"]
    roles = {
        "complete_submission",
        "accession_inventory",
        "submissions",
        "company_facts",
    }
    if (
        not isinstance(roots, Mapping)
        or set(roots) != roles
        or any(
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in roots.values()
        )
    ):
        raise ValueError("SEC candidate stage has invalid root observations")
    if "capture_revision" in parameters:
        _validate_deliveries(parameters)
    if "primary_input_revision" in parameters and (type(parameters["primary_input_revision"]) is not int
            or parameters["primary_input_revision"] != 1 or type(parameters["primary_input_plan_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", parameters["primary_input_plan_sha256"]) is None):
        raise ValueError("invalid private SEC input plan contract")
    return accession, cik, form, filename, observation_id, dict(roots)


def _candidate_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    accession: str,
    cik: str,
    observation_id: int,
    roots: Mapping[str, str],
) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "accession",
        "cik",
        "filing_observation_id",
        "roots",
        "snapshot_id",
        "snapshot_created",
    }:
        raise ValueError("SEC candidate checkpoint has an invalid shape")
    if (
        checkpoint["accession"] != accession
        or checkpoint["cik"] != cik
        or checkpoint["filing_observation_id"] != observation_id
        or checkpoint["roots"] != roots
        or isinstance(checkpoint["snapshot_id"], bool)
        or not isinstance(checkpoint["snapshot_id"], int)
        or checkpoint["snapshot_id"] <= 0
        or not isinstance(checkpoint["snapshot_created"], bool)
    ):
        raise ValueError("SEC candidate checkpoint does not match its filing")
    return dict(checkpoint)


def _inventory_names(
    payload: Mapping[str, Any], *, accession: str, cik: str
) -> set[str]:
    directory = payload.get("directory")
    if not isinstance(directory, Mapping):
        raise ValueError("SEC accession inventory has no directory object")
    expected = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    if str(directory.get("name") or "").rstrip("/") != expected:
        raise ValueError("SEC accession inventory identifies another accession")
    items = directory.get("item")
    if not isinstance(items, list) or not items:
        raise ValueError("SEC accession inventory has no resource items")
    names = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("SEC accession inventory has a malformed resource item")
        name = inline_xbrl.safe_document_name(str(item.get("name") or ""))
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("SEC accession inventory has duplicate resource names")
    return set(names)


def _recent_filing_metadata(
    payload: Mapping[str, Any], accession: str, *, nested: bool = True
) -> dict[str, str] | None:
    filings = payload.get("filings") if nested else payload
    recent = filings.get("recent") if nested and isinstance(filings, Mapping) else filings
    if not isinstance(recent, Mapping):
        raise ValueError("SEC submissions has no filing arrays")
    fields = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    arrays = {field: recent.get(field) for field in fields}
    if any(not isinstance(values, list) for values in arrays.values()):
        raise ValueError("SEC submissions filing arrays are malformed")
    lengths = {len(values) for values in arrays.values()}
    if len(lengths) != 1 or any(
        not isinstance(value, str)
        for values in arrays.values()
        for value in values
    ):
        raise ValueError("SEC submissions filing arrays are malformed")
    positions = [
        index
        for index, value in enumerate(arrays["accessionNumber"])
        if value == accession
    ]
    if not positions:
        return None
    if len(positions) != 1:
        raise ValueError("SEC submissions filing accession is ambiguous")
    position = positions[0]
    result = {
        "accession": accession,
        "form": arrays["form"][position],
        "filed": arrays["filingDate"][position],
        "report_date": arrays["reportDate"][position],
        "document": arrays["primaryDocument"][position],
    }
    if not all(result[field] for field in ("accession", "form", "filed", "document")):
        raise ValueError("SEC submissions filing metadata is incomplete")
    date.fromisoformat(result["filed"])
    if result["report_date"]:
        date.fromisoformat(result["report_date"])
    result["document"] = inline_xbrl.safe_document_name(result["document"])
    return result


def _security_identity(
    submissions: Mapping[str, Any],
    *,
    accession: str,
    form: str,
    cover_payloads: list[bytes],
) -> tuple[str, str | None, dict[str, Any] | None]:
    tickers = submissions.get("tickers")
    exchanges = submissions.get("exchanges")
    if (
        not isinstance(tickers, list)
        or not isinstance(exchanges, list)
        or len(tickers) != len(exchanges)
        or not tickers
        or any(not isinstance(value, str) or not value.strip() for value in tickers)
        or any(not isinstance(value, str) for value in exchanges)
    ):
        raise ValueError("SEC submissions listed-security arrays are malformed")
    listings = list(dict.fromkeys(
        (ticker.strip().upper(), exchange.strip() or None)
        for ticker, exchange in zip(tickers, exchanges)
    ))
    securities = []
    documents = []
    security_document = None
    for payload in cover_payloads:
        document = payload.decode("utf-8", errors="replace")
        documents.append(document)
        found = cover.securities(document)
        if found and not securities:
            securities = found
            security_document = document
    matches = []
    for ticker, exchange in listings:
        for security in securities:
            title = str(security.get("title") or "")
            if (
                cover.symbol_matches(
                    str(security.get("symbol") or ""), title, ticker
                )
                and cover.is_common_equity_security(title)
                and not cover.is_untraded_underlying(title)
            ):
                matches.append((ticker, exchange, security))
    unique = {
        (ticker, exchange, str(security.get("title") or "")): (
            ticker,
            exchange,
            security,
        )
        for ticker, exchange, security in matches
    }
    if len(unique) == 1:
        ticker, exchange, security = next(iter(unique.values()))
        title = str(security.get("title") or "")
        ratio = cover.depositary_ratio(title)
        if ratio is None and security_document is not None:
            ratio = cover.depositary_ratio(cover.text_of(security_document))
        primary_document = documents[-1] if documents else None
        if (
            ratio is None
            and primary_document is not None
            and primary_document != security_document
        ):
            ratio = cover.depositary_ratio(cover.text_of(primary_document))
        return ticker, exchange, {
            **security,
            "symbol": ticker,
            "exchange": exchange,
            "accn": accession,
            "basis_accn": accession,
            "ratio": ratio,
        }
    if len(unique) > 1:
        raise ValueError("SEC cover identifies more than one supported listed security")
    if form.split("/", 1)[0] in {"20-F", "40-F"}:
        raise ValueError("foreign SEC filer has no exact listed security on its cover")
    if len(listings) != 1:
        raise ValueError("SEC submissions primary listed security is ambiguous")
    ticker, exchange = listings[0]
    return ticker, exchange, None


def _manifest_record(document: str, url: str, payload: bytes) -> dict[str, Any]:
    return {
        "document": document,
        "url": url,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _raise_if_incomplete_inline_graph(error: ValueError) -> None:
    message = str(error)
    if " is absent from its index" in message or re.search(
        r" has 0 .* files$", message
    ):
        raise SecResourcePending("SEC Inline-XBRL graph is pending") from error


def _has_inline_companions(inventory: Mapping[str, Any]) -> bool:
    items = ((inventory.get("directory") or {}).get("item") or [])
    names = [str(item.get("name") or "").casefold() for item in items]
    return any(
        name == "filingsummary.xml"
        or name.endswith(("_htm.xml", "_pre.xml", ".xsd"))
        for name in names
    )


def _inline_artifact_role(relationship: str, role: str) -> str:
    if relationship == "direct_annual":
        return f"sec_inline_direct_annual_{role}"
    return f"sec_inline_incorporated_source_{role}"


def _parse_inline_manifest(
    manifest: Mapping[str, Any], payloads: Mapping[str, bytes]
) -> dict[str, Any]:
    annual = manifest["annual"]
    source = manifest["source"]
    records = manifest["files"]
    metadata = inline_xbrl.InlineXbrlMetadata(
        cik=manifest["cik"],
        entity_name=manifest["entity_name"],
        source_accession=source["accession"],
        source_form=source["form"],
        source_filed=source["filed"],
        source_document=source["document"],
        report_date=annual["report_date"],
        annual_accession=annual["accession"],
        annual_form=annual["form"],
        annual_filed=annual["filed"],
    )
    documents = {
        role: records[role]["document"] for role in inline_xbrl.FILE_ROLES
    }
    hashes = {role: records[role]["sha256"] for role in inline_xbrl.FILE_ROLES}
    return inline_xbrl.parse_inline_xbrl_bytes(
        metadata, payloads, documents, hashes
    )


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
    expected = {
        "accession",
        "cik",
        "filing_observation_id",
        "roots",
    }
    if not isinstance(checkpoint, Mapping) or set(checkpoint) not in (expected, expected | {"capture_revision", "root_deliveries"}):
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
    if "capture_revision" in checkpoint:
        _validate_deliveries(checkpoint)
    return dict(checkpoint)


def _combined_sources(roots, dependencies):
    sources = {(source.source_item_id, source.source_observation_id, source.occurrence_id): source for source in roots}
    for source in dependencies:
        sources.setdefault((source.source_item_id, source.source_observation_id, source.occurrence_id), source)
    return tuple(sources.values())


def _delivery_values(delivery):
    return dict(occurrence_id=delivery.occurrence_id, source_item_id=delivery.source_item_id,
        source_observation_id=delivery.source_observation_id, job_id=delivery.job_id,
        ownership_generation=delivery.ownership_generation, item_key=delivery.item_key,
        checked_at=delivery.checked_at.isoformat())


def _retained_root_source(observations, digest, *, role, parent, accession, cik, budget=None):
    stored = observations.by_sha256(digest,
        **({} if budget is None else dict(_raw_work_budget=budget)))
    item, observation = stored.item, stored.observation
    expected_parent = parent.item.source_item_id if role in {"complete_submission", "accession_inventory"} else None
    if (observation.state != "present" or observation.artifact_sha256 is None
            or observation.canonical_metadata.get("role") != role
            or observation.canonical_metadata.get("cik") != cik
            or item.source_system != "SEC" or item.issuer_source_identifier != cik
            or item.parent_source_item_id != expected_parent
            or expected_parent is not None and observation.canonical_metadata.get("accession") != accession):
        raise ValueError(f"SEC root observation does not match: {role}")
    return stored


def _retained_root_delivery(observations, stored, supplied, *, compare_instants=False, strict_revision=False):
    item, observation = stored.item, stored.observation
    delivery = observations.delivery(supplied["occurrence_id"], source_item_id=item.source_item_id,
                                      source_observation_id=observation.source_observation_id)
    actual = _delivery_values(delivery)
    if compare_instants:
        expected = dict(supplied)
        for name in ("occurrence_id", "source_item_id", "source_observation_id", "job_id", "ownership_generation"):
            _capture_id(name, actual[name])
            _capture_id(name, expected[name])
        actual["checked_at"] = _schedule_datetime("delivery checked_at", delivery.checked_at).isoformat()
        _capture_text(actual["item_key"])
        expected["checked_at"] = _schedule_datetime("checkpoint checked_at", datetime.fromisoformat(expected["checked_at"])).isoformat()
        matches = _canonical_json(actual) == _canonical_json(expected)
    else:
        matches = actual == supplied
    metadata = observation.canonical_metadata
    if (not matches or metadata.get("capture_revision") != 2
            or strict_revision and type(metadata.get("capture_revision")) is not int
            or metadata.get("capture_fidelity") != "RAW_HTTP_BYTES"):
        raise ValueError("captured delivery or raw HTTP admission differs")
    return delivery


def _capture_id(name, value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError(f"{name} must be a positive exact signed64 integer")


def _capture_sha(value):
    if type(value) is not str or len(value) != 64 or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid captured SHA256")


def _capture_text(value):
    if value is not None and (type(value) is not str or len(value) > 65536
            or "\0" in value or len(value.encode("utf-8")) > 65536):
        raise ValueError("captured reference text exceeds bounded JSON admission")
    return value


def _capture_job_stamp(job):
    _capture_id("persisted job", job.job_id)
    _capture_id("persisted occurrence", job.occurrence_id)
    _, parameters = _bounded_schedule_parameters(job.parameters)
    checkpoint = None if job.checkpoint is None else _bounded_schedule_parameters(job.checkpoint)[1]
    if (type(job.priority) is not int or not -2**31 <= job.priority < 2**31
            or type(job.max_attempts) is not int or not 0 < job.max_attempts < 2**31):
        raise ValueError("persisted resource schedule scalars are invalid")
    if type(job.ownership_generation) is not int or not 0 <= job.ownership_generation < 2**63:
        raise ValueError("persisted resource generation is invalid")
    _bounded_schedule_text("persisted kind", job.kind, 256)
    if type(job.status) is not str:
        raise ValueError("persisted resource status must be exact text")
    return dict(job_id=job.job_id, occurrence_id=job.occurrence_id, kind=job.kind,
        parameters_json=parameters, checkpoint_json=checkpoint, status=job.status,
        due_at=_schedule_datetime("resource due_at", job.due_at).isoformat(), priority=job.priority,
        max_attempts=job.max_attempts,
        deadline_at=None if job.deadline_at is None else _schedule_datetime("resource deadline", job.deadline_at).isoformat(),
        ownership_generation=job.ownership_generation, error_summary=_capture_text(job.error_summary))


def _capture_result_json(value):
    """Count the reference frontier before keeping more than one bounded envelope."""
    parts, size = [], 0
    for part in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
        size += len(part.encode("utf-8"))
        if size > _CAPTURE_REFERENCE_BYTES:
            return None, size, True
        parts.append(part)
    return json.loads("".join(parts)), size, False


def _validate_deliveries(value):
    deliveries = value["root_deliveries"]
    fields = {"occurrence_id", "source_item_id", "source_observation_id", "job_id",
              "ownership_generation", "item_key", "checked_at"}
    if (type(value["capture_revision"]) is not int or value["capture_revision"] != 2
            or not isinstance(deliveries, Mapping) or set(deliveries) != set(value["roots"])):
        raise ValueError("invalid versioned SEC capture deliveries")
    for role, delivery in deliveries.items():
        if (not isinstance(delivery, Mapping) or set(delivery) != fields
                or any(type(delivery[name]) is not int or delivery[name] <= 0
                       for name in fields - {"item_key", "checked_at"})
                or delivery["item_key"] != f"root:{role}"
                or not isinstance(delivery["checked_at"], str)):
            raise ValueError("invalid exact SEC root delivery")
        try:
            checked = datetime.fromisoformat(delivery["checked_at"])
        except ValueError as error:
            raise ValueError("invalid source delivery timestamp") from error
        if checked.utcoffset() is None:
            raise ValueError("source delivery timestamp requires timezone")


def _strict_json_object(data: bytes, label: str) -> dict[str, Any]:
    return sec_json.decode_object(data, label)


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


def _reserve_retained_schedule_codec(value, budget, maximum_bytes=1024 * 1024):
    size, utf8 = _schedule_json_size(value, budget, maximum_bytes)
    budget.reserve(work=utf8 + 4 * size)
    return size


def _retained_schedule_codec(value, budget):
    size = _reserve_retained_schedule_codec(value, budget)
    detached, canonical = _bounded_schedule_parameters(value)
    if not canonical.isascii() or len(canonical) != size:
        raise ValueError("existing schedule codec differs from retained reservation")
    budget.reserve(work=size)
    return detached, canonical.encode("utf-8")


def _reserve_retained_annual_keys(result, budget):
    from .models import RawFactOrigin
    views = (result,) + (() if result.rq_result is None or result.rq_result.snapshot is None else (result.rq_result,))
    for derived in views:
        for family in exact_annual.FAMILIES:
            for fact in getattr(derived.snapshot, family).values():
                origin = fact.exact_origin
                if isinstance(origin, RawFactOrigin):
                    # Existing _raw_observation's SHA key construction occurs per actual visit.
                    budget.reserve(work=64 + 4 * len(origin.pointer))
                    budget.reserve(work=64 + 4 * len(origin.pointer))