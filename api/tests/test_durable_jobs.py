from datetime import datetime, timedelta, timezone

import pytest

from screener.durable_jobs import DurableJobRepository, RetryPolicy


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


class NoDatabaseEngine:
    def begin(self):
        raise AssertionError("validation must happen before SQL")


def test_retry_policy_uses_bounded_exponential_cap_and_injected_jitter():
    seen_caps = []

    def jitter(cap):
        seen_caps.append(cap)
        return cap / 2

    policy = RetryPolicy(
        base_delay=timedelta(seconds=10),
        max_delay=timedelta(seconds=25),
        jitter_seconds=jitter,
    )

    assert policy.retry_at(attempts=1, now=NOW) == NOW + timedelta(seconds=5)
    assert policy.retry_at(attempts=2, now=NOW) == NOW + timedelta(seconds=10)
    assert policy.retry_at(attempts=8, now=NOW) == NOW + timedelta(seconds=12.5)
    assert seen_caps == [10, 20, 25]


@pytest.mark.parametrize("jitter", [-1, 31, float("inf"), "invalid"])
def test_retry_policy_rejects_jitter_outside_the_cap(jitter):
    policy = RetryPolicy(jitter_seconds=lambda _cap: jitter)

    with pytest.raises(ValueError, match="retry jitter"):
        policy.retry_at(attempts=1, now=NOW)


def test_retry_policy_rejects_non_integer_attempts():
    with pytest.raises(ValueError, match="attempts must be a positive integer"):
        RetryPolicy().retry_at(attempts=1.5, now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schedule_key", " ", "schedule key must not be empty"),
        ("kind", "", "job kind must not be empty"),
        ("parameters", {"bad": object()}, "parameters must contain JSON values"),
        ("parameters", {"bad": float("nan")}, "parameters must contain JSON values"),
        ("priority", True, "priority must be an integer"),
        ("max_attempts", 0, "max_attempts must be positive"),
    ],
)
def test_enqueue_validates_identity_before_sql(field, value, message):
    repository = DurableJobRepository(NoDatabaseEngine())
    values = {
        "schedule_key": "sec-current",
        "scheduled_for": NOW,
        "kind": "sec-discovery",
        "parameters": {"scope": "current"},
        "due_at": NOW,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        repository.enqueue_scheduled(**values)


def test_enqueue_requires_aware_times_and_deadline_after_due_time():
    repository = DurableJobRepository(NoDatabaseEngine())
    values = {
        "schedule_key": "sec-current",
        "scheduled_for": NOW,
        "kind": "sec-discovery",
        "parameters": {},
        "due_at": NOW,
    }

    with pytest.raises(ValueError, match="scheduled_for must be timezone-aware"):
        repository.enqueue_scheduled(
            **{**values, "scheduled_for": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValueError, match="due_at must be timezone-aware"):
        repository.enqueue_scheduled(**{**values, "due_at": NOW.replace(tzinfo=None)})
    with pytest.raises(ValueError, match="deadline_at must be after due_at"):
        repository.enqueue_scheduled(**values, deadline_at=NOW)


def test_lease_duration_and_error_summary_validate_before_sql():
    repository = DurableJobRepository(NoDatabaseEngine())

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        repository.claim_next(owner="worker", lease_duration=timedelta(0), now=NOW)
    with pytest.raises(ValueError, match="error summary must be at most"):
        repository.fail(
            object(),
            error_summary="x" * 2001,
            now=NOW,
        )
