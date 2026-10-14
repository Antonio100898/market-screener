"""Prepared current-code unplanned pair references over real isolated storage."""
from sqlalchemy import event
import pytest

from screener.figure_history import FigureHistoryRepository
from screener.shared_companies import SharedCompanyRepository
from screener import sync
from test_sec_exact_annotation_storage import setup_flow
from test_sec_completed_capture_storage import identity, rows
from test_sec_ingestion_storage import storage, database_url, NOW, _context, pytestmark


def complete_pair(storage, key):
    flow = setup_flow(storage, key)
    checkpoint = flow.stage(_context(flow.candidate, flow.jobs))
    flow.jobs.complete(flow.candidate.lease, final_checkpoint=checkpoint, now=NOW)
    return flow, checkpoint


def prohibit_full_replay(flow, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("reference recovery fetched, derived, wrote, selected current or loaded full payload/graph")
    for owner, names in ((flow.stage, ("recover_paired_job", "_load_roots")),
        (flow.stage._edgar, ("fetch",)), (flow.jobs, ("enqueue_child", "enqueue_scheduled", "save_checkpoint")),
        (flow.source, ("latest_sec_filing", "latest_item", "record", "record_with_acquisition_receipt")),
        (SharedCompanyRepository, ("snapshot", "current", "store_candidate", "store_candidate_on_connection")),
        (FigureHistoryRepository, ("load", "load_on_connection", "publication_identity_on_connection", "store_on_connection")),
        (sync, ("_derive_evidence_result", "build_snapshot"))):
        for name in names:
            monkeypatch.setattr(owner, name, denied)


def test_actual_current_unplanned_succeeded_pair_references_without_full_replay(storage, monkeypatch):
    flow, checkpoint = complete_pair(storage, "pair-reference")
    before = rows(flow.engine)
    prohibit_full_replay(flow, monkeypatch)
    statements = []
    def projection_guard(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
        assert not any(name in statement for name in ("canonical_payload", "observation_bindings", "contract_details",
            "figure_selection", "figure_dependency", "figure_bundle_coverage", "document_evidence"))
    event.listen(flow.engine, "before_cursor_execute", projection_guard)
    try:
        result = flow.stage.recover_completed_pair_references(**identity(flow, "pair-reference"))
    finally:
        event.remove(flow.engine, "before_cursor_execute", projection_guard)
    assert result["status"] == "PAIR_REFERENCES_VERIFIED" and result["root_capture_bytes_verified"]
    assert result["snapshot"]["header"]["snapshot_id"] == checkpoint["snapshot_id"]
    assert result["exact"]["header"]["bundle_id"] == checkpoint["exact_bundle_id"]
    assert result["exact"]["header"]["storing_job_id"] == flow.candidate.job.job_id
    assert len(result["snapshot"]["artifacts"]) > 4 and len(result["exact"]["sources"]) == 4
    assert all(result[name] is False for name in ("snapshot_payload_verified", "snapshot_leaf_bytes_verified",
        "exact_graph_verified", "graph_digest_recomputed"))
    assert statements and rows(flow.engine) == before


def test_actual_valid_unrelated_pair_cannot_replace_resource_stage_association(storage, monkeypatch):
    flow, _ = complete_pair(storage, "pair-original")
    other, _ = complete_pair(storage, "pair-unrelated")
    unrelated = other.jobs.scheduled_job(job_id=other.candidate.job.job_id,
        occurrence_id=other.candidate.job.occurrence_id,
        schedule_key=other.resource.candidate_stage_request(other.root_claim.job, other.root_checkpoint).schedule_key,
        scheduled_for=other.candidate.job.due_at)
    before = rows(flow.engine)
    prohibit_full_replay(flow, monkeypatch)
    jobs = flow.stage._companies._jobs
    monkeypatch.setattr(jobs, "job_for_occurrence", lambda **values: unrelated)
    with pytest.raises(ValueError):
        flow.stage.recover_completed_pair_references(**identity(flow, "pair-original"))
    assert rows(flow.engine) == before
