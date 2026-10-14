"""Internal declaration admission over synthetic identities and registered bytes."""
from contextlib import nullcontext
from dataclasses import replace
import copy
from types import SimpleNamespace as NS

import pytest

from screener import publication
from screener.primary_plan_publication import PrimaryPlanPublicationPreparer, PreparedPrimaryPlan
from screener.sec_ingestion import SecCandidateStageHandler, _canonical_json
from screener.shared_companies import SnapshotArtifact
from test_primary_input_transport import registered, resolve


def identity():
    return dict(snapshot=dict(snapshot_id=71, security_id=11), artifacts=[['a'*64, 'primary_input_plan']],
        binding=dict(exact_bundle_id=81, expected_exact_run_key='sec-annual:synthetic'),
        producer=dict(job_id=1, parameters={}, checkpoint=dict(checkpoint_revision=3)),
        source_occurrences=[[21, 22, 23]], accepted_occurrences=[[21, 22, 24]], plan_sha256='a'*64)


def seal(owner, value):
    import hmac
    body = _canonical_json(value)
    return PreparedPrimaryPlan(body, value['plan_sha256'], hmac.digest(owner._preparation_key, body, 'sha256'))


@pytest.mark.parametrize('change', ['snapshot', 'plan', 'dependency', 'checkpoint', 'source', 'accepted', 'bool', 'float'])
def test_sql_recheck_refuses_changed_exact_identity(monkeypatch, change):
    owner = PrimaryPlanPublicationPreparer(None)
    value = identity(); prepared = seal(owner, value); changed = copy.deepcopy(value)
    if change == 'snapshot': changed['snapshot']['snapshot_id'] = 72
    elif change == 'plan': changed['plan_sha256'] = 'b'*64
    elif change == 'dependency': changed['artifacts'].append(['b'*64, 'primary_input_dependency'])
    elif change == 'checkpoint': changed['producer']['checkpoint']['other'] = True
    elif change == 'source': changed['source_occurrences'][0][2] = 25
    elif change == 'accepted': changed['accepted_occurrences'][0][2] = 25
    elif change == 'bool': changed['producer']['job_id'] = True
    else: changed['snapshot']['security_id'] = 11.0
    monkeypatch.setattr(owner, 'identity_on_connection', lambda *a: changed)
    with pytest.raises(ValueError, match='verified preparation outside SQL'):
        owner.require_on_connection(None, 71, 81, 'run', prepared)


def test_seal_is_repository_local_and_caller_flags_never_qualify(monkeypatch):
    first, other = PrimaryPlanPublicationPreparer(None), PrimaryPlanPublicationPreparer(None)
    value = identity()
    monkeypatch.setattr(other, 'identity_on_connection', lambda *a: value)
    for supplied in (True, {'verified_plan': True}, seal(first, value), None):
        with pytest.raises(ValueError, match='verified preparation outside SQL'):
            other.require_on_connection(None, 71, 81, 'run', supplied)
    assert other.require_on_connection(None, 71, 81, 'run', seal(other, value)) == 'a'*64


def test_legacy_no_plan_requires_no_preparation(monkeypatch):
    owner = PrimaryPlanPublicationPreparer(None)
    monkeypatch.setattr(owner, 'identity_on_connection', lambda *a: None)
    assert owner.require_on_connection(None, 71, None, None, None) is None
    with pytest.raises(ValueError, match='no longer belongs'):
        owner.require_on_connection(None, 71, None, None, seal(owner, identity()))


class Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def all(self): return self.rows
    def one(self): assert len(self.rows) == 1; return self.rows[0]


def source_connection(case, links):
    rows = [dict(source_system=s.item.source_system, issuer_source_identifier=s.item.issuer_source_identifier,
        source_item_id=s.item.source_item_id, item_kind=s.item.item_kind, parent_source_item_id=s.item.parent_source_item_id,
        source_observation_id=s.observation.source_observation_id, artifact_sha256=s.observation.artifact_sha256,
        canonical_metadata=s.observation.canonical_metadata, state=s.observation.state, observation_sha256=digest)
        for digest,s in case.stored.items()]
    def execute(query):
        sql = str(query)
        if sql.startswith('SELECT source_item.source_item_id'): return Rows([])
        if 'FROM issuer JOIN priced_security' in sql:
            return Rows([dict(source_system='SEC', source_identifier=case.plan.cik)])
        if 'FROM source_observation JOIN source_item' in sql: return Rows(rows)
        if 'FROM company_snapshot_artifact' in sql:
            return Rows([dict(content_sha256=link.content_sha256, role=link.role) for link in links])
        raise AssertionError(sql)
    return NS(execute=execute), tuple(row['source_observation_id'] for row in rows)


def test_plan_role_alone_refuses_and_raw_dependencies_remain_source_qualified(registered, monkeypatch):
    planned = resolve(registered)
    monkeypatch.setattr(publication, 'current_observation_id', lambda connection, item: next(
        s.observation.source_observation_id for s in registered.stored.values() if s.item.source_item_id == item))
    repo = publication.PublicationRepository(None)
    connection, sources = source_connection(registered, planned.artifacts)
    with pytest.raises(publication.PublicationSourceMismatch, match='verified producing pair'):
        repo._validate_sources(connection, sources, planned.security_id, 71)
    preparer = PrimaryPlanPublicationPreparer(None)
    value = identity(); value['plan_sha256'] = planned.plan_sha256
    monkeypatch.setattr(preparer, 'identity_on_connection', lambda *a: value)
    admission = (preparer, 81, 'run', seal(preparer, value))
    repo._validate_sources(connection, sources, planned.security_id, 71, primary_plan=admission)
    connection, sources = source_connection(registered, (*planned.artifacts, SnapshotArtifact('f'*64, 'primary_input_dependency')))
    with pytest.raises(publication.PublicationSourceMismatch, match='no recorded observation lineage'):
        repo._validate_sources(connection, sources, planned.security_id, 71, primary_plan=admission)


def test_pair_context_wrapper_reuses_job_owner_without_lease(monkeypatch):
    stage = object.__new__(SecCandidateStageHandler)
    job = NS(parameters={}, checkpoint={})
    monkeypatch.setattr(stage, 'recover_paired_job', lambda actual, roots: (actual, roots))
    assert stage._paired_checkpoint(NS(job=job), {'root': 'value'}) == (job, {'root': 'value'})
