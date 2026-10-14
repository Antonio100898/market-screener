"""Strict immutable acquisition contract; no storage or source access."""
from copy import deepcopy
from dataclasses import asdict, FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from screener.durable_jobs import DurableJobRepository, LeaseToken, _SOURCE_RECEIPT_PREFIX
from screener.source_observations import (
    SourceAcquisitionReceipt, SourceItemIdentity, SourceObservationRepository,
)
from screener.source_validity import SourceDeliveryRecord


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class NoStorage:
    def begin(self):
        raise AssertionError("invalid input reached SQL")

    connect = begin


def receipt():
    return SourceAcquisitionReceipt(7, "a" * 64, "inventory:ticker-map",
        SourceItemIdentity("SEC", "inventory", "ticker-map"), "b" * 64,
        SourceDeliveryRecord(13, 17, 19, 7, 3, "ticker-source", NOW))


def test_known_answer_all_seven_original_delivery_fields_roundtrip():
    original = receipt()
    value = original.to_outcome()
    assert value == dict(receipt_revision=1, producer_job_id=7,
        request_identity_sha256="a" * 64, receipt_key="inventory:ticker-map",
        source_item=dict(source_system="SEC", item_kind="inventory", source_key="ticker-map",
            issuer_source_identifier=None, parent_source_item_id=None), observation_sha256="b" * 64,
        delivery=dict(occurrence_id=13, source_item_id=17, source_observation_id=19,
            job_id=7, ownership_generation=3, item_key="ticker-source", checked_at=NOW.isoformat()))
    assert SourceAcquisitionReceipt.from_outcome(value) == original
    assert asdict(SourceAcquisitionReceipt.from_outcome(value).delivery) == asdict(original.delivery)
    with pytest.raises(FrozenInstanceError):
        original.receipt_key = "changed"


@pytest.mark.parametrize("path", [
    ("receipt_revision",), ("producer_job_id",), ("delivery", "occurrence_id"),
    ("delivery", "source_item_id"), ("delivery", "source_observation_id"),
    ("delivery", "job_id"), ("delivery", "ownership_generation"),
    ("source_item", "parent_source_item_id"),
])
@pytest.mark.parametrize("invalid", [True, 1.0, 0, -1, "1", 9223372036854775808])
def test_ids_and_revision_refuse_boolean_equal_float_and_invalid_integers(path, invalid):
    value = receipt().to_outcome()
    owner = value
    for name in path[:-1]:
        owner = owner[name]
    owner[path[-1]] = invalid
    with pytest.raises(ValueError):
        SourceAcquisitionReceipt.from_outcome(value)


@pytest.mark.parametrize("path,invalid", [
    (("receipt_key",), " changed"), (("receipt_key",), ""), (("receipt_key",), "é" * 257),
    (("request_identity_sha256",), None), (("request_identity_sha256",), "A" * 64),
    (("observation_sha256",), "b" * 63), (("delivery", "job_id"), 8),
    (("delivery", "item_key"), _SOURCE_RECEIPT_PREFIX + "fake"),
    (("delivery", "checked_at"), NOW.replace(tzinfo=None).isoformat()),
    (("delivery", "checked_at"), "2026-10-09T12:00:00Z"),
    (("source_item", "source_system"), []), (("source_item", "source_key"), "x" * 1025),
    (("source_item", "issuer_source_identifier"), "123"),
])
def test_receipt_identity_text_hash_time_and_source_boundaries(path, invalid):
    value = receipt().to_outcome()
    owner = value
    for name in path[:-1]:
        owner = owner[name]
    owner[path[-1]] = invalid
    with pytest.raises(ValueError):
        SourceAcquisitionReceipt.from_outcome(value)


@pytest.mark.parametrize("path", [(), ("source_item",), ("delivery",)])
@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_unknown_or_missing_fields_refuse(path, mutation):
    value = receipt().to_outcome()
    owner = value
    for name in path:
        owner = owner[name]
    if mutation == "extra":
        owner["caller_claimed_immutable"] = True
    else:
        owner.pop(next(iter(owner)))
    with pytest.raises(ValueError):
        SourceAcquisitionReceipt.from_outcome(value)


@pytest.mark.parametrize("overrides", [
    {"receipt_key": "x" * 513}, {"request_identity_sha256": True},
    {"item_outcome_key": _SOURCE_RECEIPT_PREFIX + "fake"},
    {"item_outcome_key": "x" * 513}, {"item": SourceItemIdentity("SEC", "inventory", "x" * 1025)},
    {"state": "present"},
])
def test_new_record_validates_before_any_writer_sql(overrides):
    values = dict(receipt_key="inventory:ticker-map", request_identity_sha256="a" * 64,
        item=SourceItemIdentity("SEC", "inventory", "ticker-map"), item_outcome_key="source",
        state="unavailable", metadata={"reason": "synthetic"}, source_url="https://www.sec.gov/map",
        detected_at=NOW)
    values.update(overrides)
    with pytest.raises(ValueError):
        SourceObservationRepository(NoStorage(), clock=lambda: NOW).record_with_acquisition_receipt(
            LeaseToken(7, "worker", 3), **values)


@pytest.mark.parametrize("job", [True, 1.0, 0])
def test_exact_getter_validates_job_before_sql(job):
    with pytest.raises(ValueError):
        SourceObservationRepository(NoStorage()).acquisition_receipt(
            producer_job_id=job, receipt_key="map", request_identity_sha256="a" * 64)


def test_mutable_outcome_cannot_insert_reserved_receipt():
    with pytest.raises(ValueError, match="immutable insertion"):
        DurableJobRepository(NoStorage()).record_item_outcome(LeaseToken(7, "worker", 3),
            item_key=_SOURCE_RECEIPT_PREFIX + "fake", status="succeeded", outcome=deepcopy(receipt().to_outcome()))


def test_maximum_utf8_keys_and_bigint_delivery_fit_fixed_receipt_envelope():
    maximum = 9223372036854775807
    original = SourceAcquisitionReceipt(maximum, "a" * 64, "😀" * 128,
        SourceItemIdentity("SEC", "resource", "😀" * 256, "0000001800", maximum), "b" * 64,
        SourceDeliveryRecord(maximum, maximum, maximum, maximum, maximum, "😀" * 128,
            datetime.max.replace(tzinfo=timezone.utc)))
    value = original.to_outcome()
    import json
    assert len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) < 8192
    assert SourceAcquisitionReceipt.from_outcome(value) == original


@pytest.mark.parametrize("field", ["receipt_key", "item_outcome_key", "source_key"])
def test_nul_acquisition_keys_refuse_before_writer_sql(field):
    values = dict(receipt_key="inventory:ticker-map", request_identity_sha256="a" * 64,
        item=SourceItemIdentity("SEC", "inventory", "ticker-map"), item_outcome_key="source",
        state="unavailable", metadata={"reason": "synthetic"}, source_url="https://www.sec.gov/map",
        detected_at=NOW)
    if field == "source_key":
        values["item"] = SourceItemIdentity("SEC", "inventory", "ticker\x00map")
    else:
        values[field] = "valid\x00suffix"
    with pytest.raises(ValueError):
        SourceObservationRepository(NoStorage(), clock=lambda: NOW).record_with_acquisition_receipt(
            LeaseToken(7, "worker", 3), **values)


def test_nul_receipt_key_refuses_before_read_sql():
    with pytest.raises(ValueError):
        SourceObservationRepository(NoStorage()).acquisition_receipt(
            producer_job_id=7, receipt_key="\x00receipt", request_identity_sha256="a" * 64)


@pytest.mark.parametrize("path", [("receipt_key",), ("source_item", "source_key"), ("delivery", "item_key")])
def test_nul_durable_receipt_keys_refuse_in_strict_decoder(path):
    value = receipt().to_outcome()
    container = value
    for name in path[:-1]:
        container = container[name]
    container[path[-1]] = "valid\x00suffix"
    with pytest.raises(ValueError):
        SourceAcquisitionReceipt.from_outcome(value)


def test_non_utc_aware_delivery_encodes_canonical_utc_and_recovers_original_instant():
    returned_time = NOW.astimezone(timezone(timedelta(hours=3)))
    original = replace(receipt(), delivery=replace(receipt().delivery, checked_at=returned_time))
    encoded = original.to_outcome()
    assert encoded["delivery"]["checked_at"] == NOW.isoformat()
    decoded = SourceAcquisitionReceipt.from_outcome(encoded)
    assert decoded == original and decoded.delivery == original.delivery
    assert decoded.delivery.checked_at == original.delivery.checked_at == NOW
    assert original.delivery.checked_at.isoformat() == "2026-10-09T15:00:00+03:00"


def test_naive_delivery_cannot_be_encoded_as_original_acquisition():
    original = replace(receipt(), delivery=replace(receipt().delivery, checked_at=NOW.replace(tzinfo=None)))
    with pytest.raises(ValueError):
        original.to_outcome()
