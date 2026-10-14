import pytest
from sqlalchemy import create_engine

from screener.durable_jobs import LeaseToken
from screener.publication import PublicationRepository, release_selection


@pytest.mark.parametrize("release_id", [True, 0, -1, "1"])
def test_pinned_selection_rejects_invalid_release_identity(release_id):
    with pytest.raises(ValueError, match="positive integer"):
        release_selection(release_id)


@pytest.mark.parametrize("key", [None, "", "  "])
def test_publication_rejects_empty_idempotency_key_before_storage(key):
    repository = PublicationRepository(create_engine("sqlite://"))
    with pytest.raises(ValueError, match="request_key"):
        repository.bootstrap(LeaseToken(1, "worker", 1), request_key=key)


def test_publication_rejects_empty_candidate_set_before_storage():
    repository = PublicationRepository(create_engine("sqlite://"))
    with pytest.raises(ValueError, match="must not be empty"):
        repository.publish(LeaseToken(1, "worker", 1), request_key="publish", candidate_ids=(), expected_parent_release_id=1)


@pytest.mark.parametrize("parent", [True, False, 0, -1, "1", 1.0])
@pytest.mark.parametrize("operation", ["register", "publish"])
def test_expected_empty_state_does_not_admit_invalid_parent_ids(parent, operation):
    repository = PublicationRepository(create_engine("sqlite://"))
    with pytest.raises(ValueError, match="positive integer"):
        if operation == "register":
            repository.register_candidate(LeaseToken(1, "worker", 1), request_key="initial", snapshot_id=1,
                expected_parent_release_id=parent, source_observation_ids=(1,), cause_type="source_change")
        else:
            repository.publish(LeaseToken(1, "worker", 1), request_key="initial", candidate_ids=(1,), expected_parent_release_id=parent)
