"""Live lease fences after real PostgreSQL lock waits; disposable fixtures only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import sleep

import pytest
from sqlalchemy import event, func, insert, select, update

from test_research_run_storage import storage, _create, _claim, pytestmark
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.postgres import (active_data_release, durable_job, durable_job_item, issuer,
    company_snapshot, research_run, source_item, source_observation)
from screener.publication import PublicationRepository
from screener.research_runs import ResearchRunRepository
from screener.shared_companies import SharedCompanyRepository
from screener.source_observations import SourceItemIdentity, SourceObservationRepository


def _wait(engine, table, identity, operation, *, expires=True, witness_text=None):
    reached = Event()
    def witness(connection, cursor, statement, parameters, context, executemany):
        matches = witness_text in statement if witness_text else (f"FROM {table.name} " in statement and "FOR UPDATE" in statement)
        if matches: reached.set()
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(select(table).where(identity).with_for_update()).first()
    event.listen(engine, 'before_cursor_execute', witness)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operation)
            try:
                assert reached.wait(5), 'operation did not reach contested lock'
                assert not future.done(), 'operation did not wait'
                sleep(0.65 if expires else 0.15)
            finally:
                released = datetime.now(timezone.utc)
                transaction.commit()
            if expires:
                with pytest.raises(LeaseLost):
                    future.result(timeout=5)
                return released
            return released, future.result(timeout=5)
    finally:
        event.remove(engine, 'before_cursor_execute', witness)
        blocker.close()


def _expire(engine, lease):
    with engine.begin() as connection:
        connection.execute(update(durable_job).where(durable_job.c.job_id == lease.job_id)
            .values(lease_expires_at=datetime.now(timezone.utc)+timedelta(seconds=0.45)))


@pytest.mark.parametrize('table_name', ['durable_job', 'research_run'])
@pytest.mark.parametrize('operation', ['begin', 'context', 'progress', 'finish'])
def test_private_operations_reject_expiry_during_last_lock_wait(storage, table_name, operation):
    engine, _, _, owner, workspace, _ = storage
    run = _create(storage); lease = _claim(engine)
    repository = ResearchRunRepository(engine)
    if operation != 'begin': repository.begin(lease, run['run_id'])
    before = repository.get(owner, workspace, run['run_id'])
    _expire(engine, lease)
    table = durable_job if table_name == 'durable_job' else research_run
    identity = table.c.job_id == lease.job_id if table_name == 'durable_job' else table.c.run_id == run['run_id']
    kwargs = {'event_key': 'expired', 'stage': 'local fixture'} if operation == 'progress' else {}
    _wait(engine, table, identity, lambda: getattr(repository, operation)(lease, run['run_id'], **kwargs))
    assert repository.get(owner, workspace, run['run_id']) == before


@pytest.mark.parametrize('operation', ['renew', 'save_checkpoint', 'record_item_outcome', 'complete', 'fail', 'defer', 'enqueue_child'])
def test_durable_mutations_reject_expiry_while_waiting_for_job(storage, operation):
    engine = storage[0]; _create(storage); lease = _claim(engine)
    repository = DurableJobRepository(engine)
    now = datetime.now(timezone.utc)
    operations = {
        'renew': lambda: repository.renew(lease, lease_duration=timedelta(seconds=20)),
        'save_checkpoint': lambda: repository.save_checkpoint(lease, {'fixture': 'expired'}),
        'record_item_outcome': lambda: repository.record_item_outcome(lease, item_key='expired', status='succeeded'),
        'complete': lambda: repository.complete(lease),
        'fail': lambda: repository.fail(lease, error_summary='expired fixture'),
        'defer': lambda: repository.defer(lease, reason='expired fixture'),
        'enqueue_child': lambda: repository.enqueue_child(lease, child_key='expired', scheduled_for=now,
            kind='local_fixture', parameters={}, due_at=now),
    }
    _expire(engine, lease)
    _wait(engine, durable_job, durable_job.c.job_id == lease.job_id, operations[operation])
    with engine.connect() as connection:
        job = connection.execute(select(durable_job).where(durable_job.c.job_id == lease.job_id)).mappings().one()
        assert job['status'] == 'running' and job['checkpoint'] is None and job['attempts'] == 1
        assert connection.scalar(select(func.count()).select_from(durable_job_item)) == 0


def test_renewal_after_valid_wait_uses_post_wait_timestamp(storage):
    engine=storage[0]; _create(storage); lease=_claim(engine, seconds=10)
    duration=timedelta(seconds=10)
    released, renewed = _wait(engine, durable_job, durable_job.c.job_id == lease.job_id,
        lambda: DurableJobRepository(engine).renew(lease, lease_duration=duration), expires=False)
    assert renewed.updated_at >= released and renewed.heartbeat_at >= released
    assert renewed.lease_expires_at >= released + duration


def test_item_wait_is_refenced_before_outcome_write(storage):
    engine=storage[0]; _create(storage); lease=_claim(engine)
    repository=DurableJobRepository(engine)
    repository.record_item_outcome(lease, item_key='retained', status='succeeded', outcome={'value': 1})
    _expire(engine, lease)
    _wait(engine, durable_job_item, durable_job_item.c.job_id == lease.job_id,
        lambda: repository.record_item_outcome(lease, item_key='retained', status='succeeded', outcome={'value': 2}))
    with engine.connect() as connection:
        assert connection.scalar(select(durable_job_item.c.outcome)) == {'value': 1}


def test_publication_active_pointer_wait_is_refenced(storage):
    engine, _, _, _, _, release=storage
    _create(storage); lease=_claim(engine)
    with engine.begin() as connection:
        connection.execute(update(active_data_release).where(active_data_release.c.singleton_id == 1).values(release_id=release))
    _expire(engine, lease)
    _wait(engine, active_data_release, active_data_release.c.singleton_id == 1,
        lambda: PublicationRepository(engine).bootstrap(lease, request_key='expired-publication'))
    assert PublicationRepository(engine).active().release_id == release


def test_source_item_wait_is_refenced_before_observation_write(storage):
    engine=storage[0]; _create(storage); lease=_claim(engine)
    repository=SourceObservationRepository(engine)
    item=SourceItemIdentity('SEC', 'filing', 'lease-wait-fixture')
    def record(revision):
        return repository.record(lease, item=item, item_outcome_key=f'fixture:{revision}', state='pending',
            metadata={'revision':revision}, source_url='https://www.sec.gov/lease-wait-fixture')
    original=record(1)
    _expire(engine, lease)
    _wait(engine, source_item, source_item.c.source_item_id == original.item.source_item_id, lambda: record(2),
        witness_text='INSERT INTO source_item')
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observation)) == 1


def test_shared_candidate_wait_rolls_back_expired_worker_writes(storage):
    engine=storage[0]; _create(storage); lease=_claim(engine)
    with engine.begin() as connection:
        issuer_id=connection.scalar(insert(issuer).values(source_system='SEC', source_identifier='0000001800')
            .returning(issuer.c.issuer_id))
    _expire(engine, lease)
    def candidate():
        return SharedCompanyRepository(engine).store_candidate(lease, issuer_source='SEC', issuer_identifier='1800',
            security_identifier='sec:0000001800:lease-wait-fixture', ticker='ABT', security_title='Common Stock',
            source_accession='lease-wait-fixture', security_basis='PRIMARY_COMMON_SHARE', engine_revision=187,
            canonical_payload={'fixture_value':100}, artifacts=())
    _wait(engine, issuer, issuer.c.issuer_id == issuer_id, candidate, witness_text='INSERT INTO priced_security')
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(company_snapshot)) == 0
