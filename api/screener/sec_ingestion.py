from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .artifacts import ArtifactRepository, store_evidence
from .durable_jobs import DurableJobRepository
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
_MAX_DAYS = 31


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
