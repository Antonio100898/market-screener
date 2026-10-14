"""Explicit occurrence checks through real owners over labelled fixture bodies."""
from dataclasses import replace
import hashlib
import json
import os

import pytest
from sqlalchemy import select, func

from screener import automatic_ingestion as owner
from screener.postgres import durable_job, exact_figure_bundle, source_check_occurrence
from screener.durable_jobs import DurableJobRepository, RetryPolicy
from datetime import timedelta
from screener.sec_ingestion import SecResourceFetchHandler
from test_publication_storage import storage, pytestmark
from test_automatic_ingestion_storage import (
    RetainedTransport, fixture_responses, plan, pointers, proof_directory, wait,
    test_actual_composed_process_kill_and_reclaim_keeps_exact_checkpoint as native_recovery,
)
from test_sec_ingestion_storage import _resource_urls


@pytest.mark.parametrize('kind', ['sec-recent-discovery-v3', 'sec-index-reconciliation-v3'])
def test_distinct_occurrences_with_identical_index_and_roots_capture_twice(storage, tmp_path, kind):
    engine = storage[0]; s3 = owner.create_s3_client()
    original = pointers(engine)
    try:
        for number in range(2):
            settings, _ = plan(tmp_path, kind=kind, key=f'{kind}-{number}')
            edgar = RetainedTransport(fixture_responses())
            assembly = owner.start_automatic_ingestion(settings, engine=engine, s3=s3, edgar=edgar)
            try:
                rows = wait(engine, lambda rows: len(rows) == (number + 1) * 3
                            and all(r['status'] == 'succeeded' for r in rows))
                assert set(_resource_urls().values()) <= set(edgar.urls)
            finally:
                owner.stop_automatic_ingestion(assembly)
        roots = sorted((r for r in rows if r['kind'] == 'sec-resource-fetch-v2'), key=lambda r: r['job_id'])
        assert len(roots) == 2
        assert roots[0]['parameters'] == roots[1]['parameters']
        assert roots[0]['checkpoint']['roots'] == roots[1]['checkpoint']['roots']
        for role in _resource_urls():
            earlier, later = [r['checkpoint']['root_deliveries'][role] for r in roots]
            assert earlier['source_item_id'] == later['source_item_id']
            assert earlier['source_observation_id'] == later['source_observation_id']
            assert earlier['occurrence_id'] < later['occurrence_id']
        assert pointers(engine) == original
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 2
        (proof_directory(tmp_path) / f'mutable-identical-{kind}.json').write_text(json.dumps({
            'label': 'Synthetic raw HTTP fixtures; actual source/capture/pair owners',
            'kind': kind, 'root_checkpoints': [r['checkpoint'] for r in roots],
            'current_active': original}, indent=2))
    finally:
        s3.close()


@pytest.mark.parametrize('role', ['company_facts', 'submissions', 'complete_submission'])
def test_identical_index_with_one_root_A_B_A_keeps_three_exact_deliveries(storage, tmp_path, monkeypatch, role):
    engine = storage[0]; s3 = owner.create_s3_client(); captures = []
    resource = SecResourceFetchHandler.__call__
    # Exercise same leased completed capture before the runtime's terminal completion.
    def replay_capture(self, context):
        checkpoint = resource(self, context)
        def no_fetch(*args, **kwargs):
            raise AssertionError('completed capture replay fetched a source')
        fetch = self._edgar.fetch
        self._edgar.fetch = no_fetch
        try:
            assert resource(self, replace(context, job=replace(context.job, checkpoint=checkpoint))) == checkpoint
        finally:
            self._edgar.fetch = fetch
        captures.append(checkpoint)
        return checkpoint
    monkeypatch.setattr(SecResourceFetchHandler, '__call__', replay_capture)
    original = pointers(engine); body_proof = []
    try:
        for number, version in enumerate(('A', 'B', 'A')):
            responses = fixture_responses(); url = _resource_urls()[role]; response = responses[url]
            if version == 'B':
                if role == 'complete_submission':
                    body = response.data.replace(b'fixture filing', b'fixture revised filing')
                else:
                    payload = json.loads(response.data); payload['fixture_revision'] = 'B'
                    body = json.dumps(payload).encode()
                responses[url] = replace(response, data=body)
            body_proof.append({'version': version, 'sha256': hashlib.sha256(responses[url].data).hexdigest(),
                               'bytes': len(responses[url].data)})
            settings, _ = plan(tmp_path, kind='sec-recent-discovery-v3', key=f'{role}-{number}')
            edgar = RetainedTransport(responses)
            assembly = owner.start_automatic_ingestion(settings, engine=engine, s3=s3, edgar=edgar)
            try:
                rows = wait(engine, lambda rows: len(rows) == (number + 1) * 3
                            and all(r['status'] == 'succeeded' for r in rows))
            finally:
                owner.stop_automatic_ingestion(assembly)
        assert len(captures) == 3 and len({json.dumps(c['root_deliveries'], sort_keys=True) for c in captures}) == 3
        deliveries = [c['root_deliveries'][role] for c in captures]
        assert deliveries[0]['source_observation_id'] == deliveries[2]['source_observation_id'] != deliveries[1]['source_observation_id']
        assert deliveries[0]['occurrence_id'] < deliveries[1]['occurrence_id'] < deliveries[2]['occurrence_id']
        with engine.connect() as connection:
            actual = connection.execute(select(source_check_occurrence).where(
                source_check_occurrence.c.occurrence_id.in_([d['occurrence_id'] for d in deliveries]))).mappings().all()
            assert len(actual) == 3
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 3
        assert pointers(engine) == original
        (proof_directory(tmp_path) / f'mutable-root-{role}-A-B-A.json').write_text(json.dumps({
            'label': 'Synthetic nonfinancial raw content revision, no native financial claim',
            'role': role, 'bodies': body_proof, 'root_checkpoints': captures,
            'actual_checks': [dict(row) for row in actual], 'current_active': original}, default=str, indent=2))
    finally:
        s3.close()


@pytest.mark.parametrize('point', ['capture', 'pair'])
@pytest.mark.skipif(os.name != 'nt', reason='Native ownership proof requires Windows CIM/PowerShell')
def test_occurrence_bound_native_process_reclaims_exact_capture_or_pair(storage, tmp_path, point):
    native_recovery(storage, tmp_path, point, producer_kind='sec-recent-discovery-v3')


def test_committed_child_then_producer_retry_keeps_one_occurrence_request(storage, tmp_path, monkeypatch):
    engine = storage[0]; s3 = owner.create_s3_client(); inserted = []
    enqueue = DurableJobRepository.enqueue_child
    def interrupted(self, lease, **kwargs):
        child = enqueue(self, lease, **kwargs)
        if kwargs['child_key'].startswith('sec-resource-fetch-v2:occurrence:'):
            inserted.append(child.job_id)
            if len(inserted) == 1:
                raise RuntimeError('fixture interruption after actual child commit')
        return child
    monkeypatch.setattr(DurableJobRepository, 'enqueue_child', interrupted)
    monkeypatch.setattr(owner, 'DurableJobRepository', lambda engine: DurableJobRepository(engine,
        retry_policy=RetryPolicy(base_delay=timedelta(milliseconds=1),
                               max_delay=timedelta(milliseconds=1), jitter_seconds=lambda _: 0)))
    settings, _ = plan(tmp_path, kind='sec-recent-discovery-v3', key='producer-retry')
    assembly = owner.start_automatic_ingestion(settings, engine=engine, s3=s3,
                                              edgar=RetainedTransport(fixture_responses()))
    try:
        rows = wait(engine, lambda rows: len(rows) == 3 and all(r['status'] == 'succeeded' for r in rows))
        producer = next(r for r in rows if r['kind'] == 'sec-recent-discovery-v3')
        assert producer['attempts'] == 2 and len(inserted) == 2 and inserted[0] == inserted[1]
        assert len([r for r in rows if r['kind'] == 'sec-resource-fetch-v2']) == 1
        assert pointers(engine) == dict(current=0, releases=0, active=None)
        (proof_directory(tmp_path) / 'mutable-producer-committed-child-retry.json').write_text(json.dumps({
            'label': 'Actual committed enqueue then producer interruption/reclaim; synthetic transport',
            'producer_job_id': producer['job_id'], 'producer_occurrence_id': producer['occurrence_id'],
            'generation': producer['ownership_generation'], 'attempts': producer['attempts'],
            'resource_job_id': inserted[0]}, indent=2))
    finally:
        owner.stop_automatic_ingestion(assembly); s3.close()
