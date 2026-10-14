import pytest

from screener.source_validity import SourceValidityRepository, dependency_status


@pytest.mark.parametrize("current,state,reason,occurrence,expected", [
    (1, "present", None, 1, "CAPTURED_MATCH"),
    (2, "present", "metadata_edited", 2, "REVALIDATION_REQUIRED"),
    (2, "pending", "not_found", 2, "INPUTS_PENDING"),
    (2, "removed", "withdrawn", 2, "WITHDRAWN"),
    (2, "removed", "absent_from_rebuilt_quarter", 2, "SOURCE_REMOVED_REVIEW_REQUIRED"),
    (2, "unavailable", "viewing_period_expired", 2, "SOURCE_ACCESS_UNAVAILABLE"),
    (2, "unavailable", "not_disclosed", 2, "SOURCE_ACCESS_UNAVAILABLE"),
    (2, "unavailable", "absent_from_reconciled_list", 2, "SOURCE_ACCESS_UNAVAILABLE"),
    (None, None, None, None, "NOT_ASSESSED"),
    (1, "present", None, None, "NOT_ASSESSED"),
])
def test_source_status_does_not_guess_numeric_invalidation(current, state, reason, occurrence, expected):
    assert dependency_status(1, current, state, {"reason": reason}, occurrence) == expected


@pytest.mark.parametrize("release,ids", [(0, [1]), (1, []), (1, [True]), (1, [1] * 301)])
def test_assessment_is_bounded_before_database_access(release, ids):
    with pytest.raises(ValueError):
        SourceValidityRepository(None).assess(release, ids)
