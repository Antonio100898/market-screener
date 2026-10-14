"""Verification-only composed process. All HTTP responses are labelled fixtures."""
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic,sleep

root=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(root/'api'),str(root/'api/tests'),str(root/'api/tests/integration')]
from screener import automatic_ingestion as owner,exact_annual,normalize,sync
from screener.durable_jobs import DurableJobRepository,RetryPolicy
from screener.job_runtime import DurableJobRuntime
from screener.sec_ingestion import SecResourceFetchHandler,SecCandidateStageHandler
from screener.source_observations import SourceObservationRepository
from sqlalchemy import select
from screener.postgres import durable_job
from test_automatic_ingestion_storage import RetainedTransport,fixture_responses
from test_sec_ingestion_storage import _resource_urls

phase=sys.argv[1];marker=Path(sys.argv[2]);responses=fixture_responses()
edgar=RetainedTransport(responses)
fetch=edgar.fetch
def no(*args,**kwargs):raise AssertionError('completed pair recovery attempted HTTP/latest/selector/build/emitter')
if phase=='recover-pair':
    edgar.fetch=no
    SourceObservationRepository.latest_sec_filing=no
    for name in ('_derive_result','_derive_evidence_result','build_snapshot'):
        setattr(sync,name,no)
    normalize.build_snapshot=no;exact_annual.prepare=no
elif phase=='recover-capture':
    roots=set(_resource_urls().values())
    def leaves_only(url):
        assert url not in roots,'completed capture roots fetched again'
        return fetch(url)
    edgar.fetch=leaves_only
owner.EdgarClient=lambda **kw:edgar
owner.DurableJobRepository=lambda engine:DurableJobRepository(engine,retry_policy=RetryPolicy(
    base_delay=timedelta(milliseconds=1),max_delay=timedelta(seconds=1),jitter_seconds=lambda _:0))
owner.DurableJobRuntime=lambda jobs,source,handlers,**kw:DurableJobRuntime(jobs,source,handlers,
    worker_count=1,poll_interval=timedelta(milliseconds=30),lease_duration=timedelta(seconds=2),
    heartbeat_interval=timedelta(milliseconds=200))
if phase in ('kill-capture','kill-pair'):
    handler=SecResourceFetchHandler if phase=='kill-capture' else SecCandidateStageHandler
    original=handler.__call__
    def parked(self,context):
        checkpoint=original(self,context)
        marker.write_text(json.dumps({'phase':phase,'process_id':os.getpid(),'job_id':context.job.job_id,
            'generation':context.lease.generation,'checkpoint':checkpoint},default=str))
        Event().wait()
    handler.__call__=parked
assembly=owner.start_automatic_ingestion()
try:
    deadline=monotonic()+15
    while monotonic()<deadline:
        with assembly.runtime._repository.engine.connect() as connection:
            rows=connection.execute(select(durable_job)).mappings().all()
        if phase.startswith('recover') and len(rows)==3 and all(row['status']=='succeeded' for row in rows):
            candidate=next(row for row in rows if row['kind']=='sec-candidate-stage')
            marker.write_text(json.dumps({'phase':phase,'job_id':candidate['job_id'],
                'generation':candidate['ownership_generation'],'checkpoint':candidate['checkpoint'],
                'transport_urls':edgar.urls,'runtime_errors':assembly.runtime.errors},default=str))
            break
        if any(row['status']=='failed' for row in rows):
            raise AssertionError([(row['kind'],row['error_summary']) for row in rows])
        sleep(.03)
    else:raise AssertionError('composed process did not finish within15seconds')
finally:
    owner.stop_automatic_ingestion(assembly)
