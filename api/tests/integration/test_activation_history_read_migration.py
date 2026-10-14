"""Measured forward0019 access index; old truth and payload rows stay intact."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from test_initial_publication_migration import previous_head, pytestmark
from test_publication_witness_migration import populate_legacy


def state(engine):
    with engine.connect() as connection:
        return {'version':connection.scalar(text('SELECT version_num FROM alembic_version')),
            'rows':{name:connection.scalars(text(f'SELECT to_jsonb(t) FROM {name} t ORDER BY to_jsonb(t)::text')).all()
                for name in ('data_release','release_company','publication_candidate','publication_candidate_source',
                    'release_company_lineage','release_company_source','active_data_release','source_observation',
                    'source_check_occurrence','source_item_current','figure_activation_transition')},
            'indexes':connection.scalars(text("SELECT indexdef FROM pg_indexes WHERE tablename='source_check_occurrence' ORDER BY indexname")).all()}


@pytest.mark.parametrize('populated',[False,True])
def test_empty_populated0018_to_actual_head_preserves_all_rows_and_parity(previous_head,populated):
    engine,config=previous_head
    command.upgrade(config,'20261004_0018')
    if populated: populate_legacy(engine)
    before=state(engine)
    command.upgrade(config,'head')
    after=state(engine)
    assert after['version']==ScriptDirectory.from_config(config).get_current_head()
    assert after['rows']==before['rows']
    assert set(before['indexes'])<=set(after['indexes']) and len(after['indexes'])==len(before['indexes'])+1
    assert any('ix_source_check_item_occurrence' in index and 'occurrence_id DESC' in index for index in after['indexes'])
    command.check(config)
    print('0018 ->actual head',after['version'],'populated=',populated,'all retained source/release/history rows preserved; metadata parity passes.')


def test_actual0019_locked_populated_upgrade_rolls_back_then_retries(previous_head):
    engine,config=previous_head
    command.upgrade(config,'20261004_0018'); populate_legacy(engine)
    before=state(engine)
    with engine.connect() as blocker:
        transaction=blocker.begin(); blocker.execute(text('LOCK TABLE source_check_occurrence IN ROW EXCLUSIVE MODE'))
        started=monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(command.upgrade,config,'20261004_0019')
            with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
                deadline=monotonic()+3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not future.done(); sleep(.01)
            with pytest.raises(OperationalError,match='lock timeout'): future.result(8)
        elapsed=monotonic()-started
        assert 4.5<elapsed<8; transaction.rollback()
    assert state(engine)==before
    command.upgrade(config,'20261004_0019')
    after=state(engine)
    assert after['rows']==before['rows'] and after['version']=='20261004_0019'
    assert set(before['indexes'])<=set(after['indexes']) and len(after['indexes'])==len(before['indexes'])+1
    assert any('ix_source_check_item_occurrence' in index and 'occurrence_id DESC' in index for index in after['indexes'])
    print(f'0019 real DDL wait {elapsed:.3f}s; version/index/data rollback; valid literal0019 retry passed.')
    command.upgrade(config,'head'); command.check(config)
    current=state(engine)
    assert current['version']==ScriptDirectory.from_config(config).get_current_head()
    assert current['rows']==after['rows'] and current['indexes']==after['indexes']
    print('Separate actual-head',current['version'],'compatibility preserves literal0019 rows/index; metadata parity passes.')


def test_actual0019_large_labelled_journal_preserves_all_check_bytes(previous_head):
    import json
    from pathlib import Path
    from test_publication_storage import _source,NOW
    engine,config=previous_head; command.upgrade(config,'20261004_0018')
    parts=populate_legacy(engine); other=_source(engine,parts[1],parts[2],key='other-item')
    with engine.begin() as c:
        for observed,count in ((parts[-1],10000),(other,50000)):
            item=c.scalar(text('SELECT source_item_id FROM source_observation WHERE source_observation_id=:observed'),dict(observed=observed))
            c.execute(text("""INSERT INTO source_check_occurrence(source_item_id,source_observation_id,job_id,ownership_generation,item_key,checked_at)
                SELECT :item,:observed,:job,:generation,'labelled-load-'||:item||'-'||n,:now FROM generate_series(1,:count) n"""),
                dict(item=item,observed=observed,job=parts[2].job_id,generation=parts[2].generation,now=NOW,count=count))
        c.execute(text('ANALYZE source_check_occurrence'))
    def digest():
        with engine.connect() as c:
            return c.execute(text("SELECT count(*),md5(string_agg(to_jsonb(t)::text,'' ORDER BY occurrence_id)) FROM source_check_occurrence t")).one()
    before=digest(); start=monotonic(); command.upgrade(config,'20261004_0019'); elapsed=monotonic()-start
    assert digest()==before and before[0]==60002
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version'))=='20261004_0019'
        item=c.scalar(text('SELECT source_item_id FROM source_observation WHERE source_observation_id=:observed'),dict(observed=parts[-1]))
        plan=c.scalar(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT occurrence_id FROM source_check_occurrence WHERE source_item_id=:item ORDER BY occurrence_id DESC LIMIT 301'),dict(item=item))
    assert 'ix_source_check_item_occurrence' in json.dumps(plan)
    proof=dict(label='synthetic60002checks10kselected50kother;NOTdeployment-table-size',applied_revision='20261004_0019',
        row_count=before[0],complete_row_digest=before[1],migration_seconds=elapsed,query_plan=plan,
        ddl_limits='local5slock30sstatement;larger deployed table timings remain unverified')
    Path('../local/history-read-large-migration-proof-head0020-compatibility-2026-10-04.json').write_text(json.dumps(proof,indent=2),encoding='utf8')
    print(f'Actual0019 on60002 labelled checks: {elapsed:.3f}s; every retained check byte preserved; seek index used; not a deployment timing guarantee.')
    before_head=state(engine)
    command.upgrade(config,'head'); command.check(config)
    after_head=state(engine)
    assert after_head['version']==ScriptDirectory.from_config(config).get_current_head()
    assert after_head['rows']==before_head['rows'] and after_head['indexes']==before_head['indexes']
    assert digest()==before
    compatibility=dict(label='separate actual-head parity after literal0019 journal proof',
        from_revision='20261004_0019',actual_head=after_head['version'],row_count=before[0],
        complete_row_digest=before[1],all_retained_source_release_history_rows_preserved=True,
        occurrence_indexes_unchanged=True,metadata_parity=True,
        literal0019_receipt='history-read-large-migration-proof-head0020-compatibility-2026-10-04.json')
    Path('../local/history-read-large-head0020-parity-2026-10-04.json').write_text(json.dumps(compatibility,indent=2),encoding='utf8')
    print('Separate actual-head',after_head['version'],'60002-row journal/source/release/history preservation and parity passed; no0020 timing claim.')
