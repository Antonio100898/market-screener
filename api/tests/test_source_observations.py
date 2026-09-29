from datetime import datetime, timedelta, timezone

import pytest

from screener.durable_jobs import LeaseToken
from screener.source_observations import (
    SourceItemIdentity,
    SourceObservationRepository,
    observation_sha256,
)


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


class NoDatabaseEngine:
    def begin(self):
        raise AssertionError("validation must happen before SQL")


def _record(**overrides):
    values = {
        "lease": LeaseToken(1, "worker", 1),
        "item": SourceItemIdentity("SEC", "filing", "0001-26-000001"),
        "item_outcome_key": "0001-26-000001",
        "state": "pending",
        "metadata": {"form": "20-F"},
        "source_url": "https://www.sec.gov/Archives/0001.txt",
        "detected_at": NOW,
    }
    values.update(overrides)
    return SourceObservationRepository(NoDatabaseEngine()).record(**values)


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (SourceItemIdentity("OTHER", "filing", "x"), "source system"),
        (SourceItemIdentity("SEC", "other", "x"), "item kind"),
        (SourceItemIdentity("SEC", "filing", " "), "source key"),
        (
            SourceItemIdentity("SEC", "filing", "x", issuer_source_identifier=" "),
            "issuer source identifier",
        ),
        (
            SourceItemIdentity("SEC", "filing", "x", issuer_source_identifier="123"),
            "zero-padded CIK",
        ),
        (
            SourceItemIdentity(
                "EDINET", "filing", "x", issuer_source_identifier="12345"
            ),
            "E plus five digits",
        ),
        (
            SourceItemIdentity("SEC", "resource", "x", parent_source_item_id=0),
            "parent source item id",
        ),
    ],
)
def test_source_item_identity_validates_before_sql(item, message):
    with pytest.raises(ValueError, match=message):
        _record(item=item)


@pytest.mark.parametrize(
    "metadata",
    [
        {1: "not a JSON object key"},
        {"bad": object()},
        {"bad": float("nan")},
        {"bad": ("tuple",)},
    ],
)
def test_metadata_requires_strict_json_before_sql(metadata):
    with pytest.raises(ValueError, match="metadata"):
        _record(metadata=metadata)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/filing?api_key=secret",
        "https://example.test/filing?x-api-key=secret",
        "https://example.test/filing?subscription-key=secret",
        "https://example.test/filing?X-Amz-Signature=secret",
        "https://example.test/filing?signed_token=secret",
        "https://user:secret@example.test/filing",
    ],
)
def test_source_url_rejects_credentials_before_sql(url):
    with pytest.raises(ValueError, match="credential"):
        _record(source_url=url)


def test_state_and_evidence_rules_validate_before_sql():
    with pytest.raises(ValueError, match="require a verified artifact"):
        _record(state="present")
    with pytest.raises(ValueError, match="cannot claim an artifact"):
        _record(state="removed", artifact_sha256="a" * 64)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _record(state="present", artifact_sha256="A" * 64)


def test_canonical_hash_is_stable_for_equivalent_input():
    first = observation_sha256(
        state="present",
        metadata={"form": "20-F", "nested": {"b": 2, "a": 1}},
        artifact_sha256="a" * 64,
        source_url="HTTPS://EXAMPLE.TEST:443/a?b=2&a=1",
        etag=' "test" ',
        last_modified="Mon, 29 Sep 2026 12:00:00 GMT",
        source_published_at=NOW,
    )
    repeated = observation_sha256(
        state="present",
        metadata={"nested": {"a": 1, "b": 2}, "form": "20-F"},
        artifact_sha256="a" * 64,
        source_url="https://example.test/a?a=1&b=2",
        etag='"test"',
        last_modified="Mon, 29 Sep 2026 12:00:00 GMT",
        source_published_at=NOW.astimezone(timezone(timedelta(hours=3))),
    )

    assert first == repeated
    assert len(first) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "removed"),
        ("metadata", {"form": "20-F/A"}),
        ("artifact_sha256", "b" * 64),
        ("source_url", "https://example.test/changed"),
        ("etag", '"changed"'),
        ("last_modified", "Tue, 30 Sep 2026 12:00:00 GMT"),
        ("source_published_at", NOW + timedelta(seconds=1)),
    ],
)
def test_changed_revision_input_changes_hash(field, value):
    baseline = {
        "state": "present",
        "metadata": {"form": "20-F"},
        "artifact_sha256": "a" * 64,
        "source_url": "https://example.test/a",
        "etag": '"test"',
        "last_modified": "Mon, 29 Sep 2026 12:00:00 GMT",
        "source_published_at": NOW,
    }
    changed = {**baseline, field: value}
    if field == "state":
        changed["artifact_sha256"] = None

    assert observation_sha256(**baseline) != observation_sha256(**changed)


def test_detection_time_and_job_are_not_revision_identity():
    values = {
        "state": "pending",
        "metadata": {"form": "20-F"},
        "source_url": "https://example.test/a",
    }
    assert observation_sha256(**values) == observation_sha256(**values)


def test_optional_text_and_times_validate_before_sql():
    with pytest.raises(ValueError, match="etag must not be empty"):
        _record(etag=" ")
    with pytest.raises(ValueError, match="source_published_at must be timezone-aware"):
        _record(source_published_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="detected_at must be timezone-aware"):
        _record(detected_at=NOW.replace(tzinfo=None))
