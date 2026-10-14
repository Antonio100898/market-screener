"""Declared cohort mechanics with real owners; financial and market values are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text

from screener.complete_accounting import load_accounting
from screener.display_publication import canonical_bytes
from screener.durable_jobs import LeaseLost
from screener.postgres import (data_release, release_company, release_candidate, release_company_source,
                               durable_job_item, figure_activation_transition, source_item)
from screener.company_query_schema import release_company_projection
from screener.publication import (PublicationRepository, PublicationIdentityConflict,
                                  StalePublicationParent, StalePublicationSource)
from test_publication_storage import storage, _claim, _company, _source, NOW, LEASE, pytestmark
from test_display_publication_storage import _input


@pytest.fixture
def cohort(storage):
    engine, artifact = storage
    lease = _claim(engine, 'complete-cohort')
    original = _company(engine, artifact)
    inherited = _company(engine, artifact, ticker='SECOND', identifier='2')
    repo = PublicationRepository(engine, clock=lambda: NOW)
    parent = repo.bootstrap(lease, request_key='initial')
    source_a = _source(engine, artifact, lease)
    source_b = _source(engine, artifact, lease, identifier='2', key='filing-2')
    replacement = _company(engine, artifact, value=90, lease=lease)
    candidate = repo.register_candidate(lease, request_key='candidate', snapshot_id=replacement.snapshot_id,
        expected_parent_release_id=parent.release_id, source_observation_ids=(source_a,), cause_type='source_change')
    preparer, objects, a = _input(storage, replacement, source_a)
    _, _, b = _input(storage, inherited, source_b, ticker='SECOND', cik='0000000002')
    def binding(snapshot):
        return dict(security_id=snapshot.security_id, snapshot_id=snapshot.snapshot_id, exact_bundle_id=None)
    value = dict(accounting_revision=1, expected_parent_release_id=parent.release_id,
        engine_identity_sha256='a' * 64, engine_identity_status='DECLARED_NOT_VERIFIED',
        universe=[dict(key=key, artifact_sha256s=[artifact.content_sha256]) for key in ('A', 'B', 'unsupported')],
        outcomes=[dict(input_key='A', status='SELECTED', selection=binding(replacement)),
                  dict(input_key='B', status='SELECTED', selection=binding(inherited)),
                  dict(input_key='unsupported', status='UNSUPPORTED', reason='Retained synthetic input has no supported context')],
        parent_members=[binding(original), binding(inherited)],
        selected_members=[dict(**binding(replacement), disposition='REPLACED', candidate_id=candidate.candidate_id),
                          dict(**binding(inherited), disposition='RETAINED')], retirements=[])
    def prepare(accounting=None, companies=None):
        return preparer.prepare_complete(companies or [b, a], accounting=load_accounting(canonical_bytes(accounting or value)),
            capture_mode='caller_captured_enrichment', limitations=['Synthetic financial/market output; ledger mechanics only'])
    return dict(engine=engine, artifact=artifact, lease=lease, repo=repo, parent=parent, original=original,
                inherited=inherited, replacement=replacement, candidate=candidate, source_a=source_a,
                source_b=source_b, preparer=preparer, objects=objects, a=a, b=b, value=value, prepare=prepare)


def publish(c, prepared, key='complete'):
    return c['repo'].publish_complete_display(c['lease'], request_key=key,
        expected_parent_release_id=c['parent'].release_id, prepared=prepared, object_store=c['objects'])


def state(c):
    with c['engine'].connect() as connection:
        return {table.name: connection.scalar(select(func.count()).select_from(table)) for table in
            (data_release, release_company, release_candidate, release_company_source, release_company_projection,
             durable_job_item, figure_activation_transition)}


def test_complete_accounting_selects_full_parent_and_replays_after_later_source_change(cohort):
    c = cohort
    prepared = c['prepare']()
    release = publish(c, prepared)
    assert release.member_count == 2 and release.input_bundle_sha256 == prepared.input_bundle_sha256
    manifest, rows = c['preparer'].verify(prepared)
    assert len(rows) == 2
    assert manifest['complete_accounting']['engine_identity_status'] == 'DECLARED_NOT_VERIFIED'
    assert manifest['complete_accounting']['outcomes'][2]['selection'] is None
    assert manifest['complete_accounting']['outcomes'][2]['reason'] == c['value']['outcomes'][2]['reason']
    with c['engine'].connect() as connection:
        members = dict(connection.execute(select(release_company.c.security_id, release_company.c.snapshot_id)
            .where(release_company.c.release_id == release.release_id)).all())
    assert members == {c['replacement'].security_id: c['replacement'].snapshot_id,
                       c['inherited'].security_id: c['inherited'].snapshot_id}
    _source(c['engine'], c['artifact'], c['lease'], state='unavailable', revision=2, detected_at=NOW + timedelta(seconds=1))
    before = state(c)
    assert publish(c, prepared) == release
    assert state(c) == before


@pytest.mark.parametrize('change', ['build', 'reason', 'input_key'])
def test_same_request_cannot_change_declared_execution_input_or_unresolved_outcome(cohort, change):
    c = cohort
    publish(c, c['prepare']())
    changed = json.loads(canonical_bytes(c['value']))
    if change == 'build':
        changed['engine_identity_sha256'] = 'd' * 64
    elif change == 'reason':
        changed['outcomes'][2]['reason'] = 'Different retained diagnostic'
    else:
        changed['universe'][2]['key'] = changed['outcomes'][2]['input_key'] = 'different-input'
    prepared = c['prepare'](changed)
    before = state(c)
    with pytest.raises(PublicationIdentityConflict):
        publish(c, prepared)
    assert state(c) == before


def test_actual_locked_parent_blocks_self_consistent_omission_and_wrong_binding(cohort):
    c = cohort
    before = state(c)
    partial = json.loads(canonical_bytes(c['value']))
    partial['universe'].pop(1); partial['outcomes'].pop(1)
    partial['parent_members'].pop(1); partial['selected_members'].pop(1)
    prepared = c['prepare'](partial, [c['a']])
    with pytest.raises(ValueError, match='actual locked parent'):
        publish(c, prepared, 'omitted-parent')
    wrong = json.loads(canonical_bytes(c['value']))
    wrong['parent_members'][0]['snapshot_id'] = c['replacement'].snapshot_id
    with pytest.raises(ValueError, match='actual locked parent'):
        publish(c, c['prepare'](wrong), 'wrong-parent-binding')
    assert c['repo'].active() == c['parent'] and state(c) == before


def test_old_delta_retains_parent_member_and_complete_router_does_not_accept_old_manifest(cohort):
    c = cohort
    legacy = c['preparer'].prepare([c['a'], c['b']], capture_mode='caller_captured_enrichment', limitations=['Fixture'])
    with pytest.raises(ValueError, match='complete display contract'):
        publish(c, legacy)
    complete = c['prepare']()
    with pytest.raises(ValueError, match='publish_complete_display'):
        c['repo'].publish_display(c['lease'], request_key='wrong-route', expected_parent_release_id=c['parent'].release_id,
            candidate_ids=[c['candidate'].candidate_id], prepared=complete, object_store=c['objects'])
    child = c['repo'].publish(c['lease'], request_key='old-delta', expected_parent_release_id=c['parent'].release_id,
                              candidate_ids=[c['candidate'].candidate_id])
    assert child.member_count == 2
    assert c['repo'].active() == child
    with pytest.raises(StalePublicationParent):
        publish(c, complete, 'stale-parent')
    assert state(c)['data_release'] == 2


def test_complete_changed_source_and_final_lease_failure_roll_back(cohort, monkeypatch):
    c = cohort
    prepared = c['prepare']()
    before = state(c)
    from screener.figure_history import FigureHistoryRepository
    original = FigureHistoryRepository.record_activation_on_connection
    def late_expiry(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        c['repo']._clock = lambda: NOW + LEASE
        return result
    with monkeypatch.context() as patch:
        patch.setattr(FigureHistoryRepository, 'record_activation_on_connection', late_expiry)
        with pytest.raises(LeaseLost):
            publish(c, prepared)
    c['repo']._clock = lambda: NOW
    assert c['repo'].active() == c['parent'] and state(c) == before
    _source(c['engine'], c['artifact'], c['lease'], state='unavailable', revision=2, detected_at=NOW + timedelta(seconds=1))
    after_source = state(c)
    with pytest.raises(StalePublicationSource):
        publish(c, prepared, 'stale-source')
    assert c['repo'].active() == c['parent'] and state(c) == after_source


@pytest.mark.parametrize('expire', [False, True])
def test_complete_waits_for_actual_source_lock_then_checks_fresh_lease(cohort, expire):
    c = cohort
    prepared = c['prepare']()
    before = state(c)
    blocker = c['engine'].connect()
    transaction = blocker.begin()
    blocker.execute(select(source_item.c.source_item_id).order_by(source_item.c.source_item_id).with_for_update()).all()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(publish, c, prepared)
            try:
                deadline, blocked = monotonic() + 5, False
                while monotonic() < deadline:
                    with c['engine'].connect() as connection:
                        blocked = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%source_item%' AND query LIKE '%FOR UPDATE%')"))
                    if blocked:
                        break
                    sleep(.01)
                assert blocked and not future.done()
                if expire:
                    c['repo']._clock = lambda: NOW + LEASE
            finally:
                transaction.rollback()
            if expire:
                with pytest.raises(LeaseLost):
                    future.result(10)
            else:
                assert future.result(10).member_count == 2
    finally:
        blocker.close()
    if expire:
        assert c['repo'].active() == c['parent'] and state(c) == before
    else:
        assert state(c)['data_release'] == 2
