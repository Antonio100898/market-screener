"""Exact scheduling declarations and readonly identity; no source authority or SQL."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from screener.durable_jobs import DurableJobRepository, JobIdentityConflict, _bounded_schedule_parameters, _bounded_schedule_text
from screener.sec_ingestion import SecResourceFetchHandler
from test_sec_ingestion import _job, NOW


ROLES=("complete_submission","accession_inventory","submissions","company_facts")


def inputs(capture=2):
    params=dict(accession="0000001234-26-000001",cik="0000001234",form="10-K",
        filename="edgar/data/1234/0000001234-26-000001.txt",observation_id=9)
    job=replace(_job(params,kind="sec-resource-fetch-v2" if capture==2 else "sec-resource-fetch"),
        job_id=7,occurrence_id=8,priority=30)
    roots={role:chr(ord("a")+i)*64 for i,role in enumerate(ROLES)}
    checkpoint=dict(accession=params["accession"],cik=params["cik"],filing_observation_id=9,roots=roots)
    if capture==2:
        checkpoint.update(capture_revision=2,root_deliveries={role:dict(occurrence_id=101+i,
            source_item_id=201+i,source_observation_id=301+i,job_id=7,ownership_generation=1,
            item_key="root:"+role,checked_at="2026-09-29T15:00:00+03:00") for i,role in enumerate(ROLES)})
    return job,checkpoint


class NoAccess:
    def __getattr__(self,name):
        raise AssertionError("scheduling declaration used source/object access")

    def connect(self):
        raise AssertionError("invalid input reached SQL")


class ReturnedJobs:
    def __init__(self,record):
        self.record,self.calls=record,[]

    def enqueue_child(self,lease,**values):
        self.calls.append((lease,deepcopy(values)))
        return self.record


def handler(jobs=None):
    return SecResourceFetchHandler(edgar=NoAccess(),object_store=NoAccess(),artifacts=NoAccess(),
        observations=NoAccess(),jobs=jobs or NoAccess(),clock=lambda:NOW)


class RowEngine:
    def __init__(self,row):
        self.row,self.statements=row,[]

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self,*args):
        return False

    def execute(self,statement):
        self.statements.append(statement)
        return self

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


def lookup_values():
    return dict(schedule_key="sec-candidate-stage:exact",scheduled_for=NOW,kind="sec-candidate-stage",
        parameters={"integer":1,"nested":{"checked_at":"2026-09-29T15:00:00+03:00"}},
        due_at=NOW,priority=30,max_attempts=3,deadline_at=None)


def row():
    value=asdict(replace(_job(lookup_values()["parameters"],kind="sec-candidate-stage"),job_id=73,occurrence_id=42))
    value["matched_occurrence_id"]=42
    return value


@pytest.mark.parametrize("capture,planned",[(1,None),(2,None),(2,"e"*64)])
def test_public_descriptor_preserves_original_key_parameters_and_implicit_defaults(capture,planned):
    job,checkpoint=inputs(capture)
    declaration=handler().candidate_stage_request(job,checkpoint,primary_input_plan_sha256=planned)
    # Independent known input encoder is the legacy recorded JSON domain, not a new UTC DTO.
    contract=deepcopy(checkpoint["roots"]) if capture==1 else dict(roots=checkpoint["roots"],
        capture_revision=2,root_deliveries=checkpoint["root_deliveries"])
    if planned is not None:
        contract.update(primary_input_revision=1,primary_input_plan_sha256=planned)
    expected_sha=hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert declaration.schedule_key==f"sec-candidate-stage:{job.parameters['cik']}:{job.parameters['accession']}:{expected_sha}"
    assert declaration.scheduled_for==declaration.due_at==job.due_at and declaration.priority==job.priority
    assert declaration.kind=="sec-candidate-stage" and declaration.max_attempts==3 and declaration.deadline_at is None
    expected={**job.parameters,"roots":checkpoint["roots"]}
    if capture==2:expected.update(capture_revision=2,root_deliveries=checkpoint["root_deliveries"])
    if planned is not None:expected.update(primary_input_revision=1,primary_input_plan_sha256=planned)
    assert declaration.parameters==expected
    expected=deepcopy(expected)
    if capture==2:
        assert declaration.parameters["root_deliveries"]["company_facts"]["checked_at"].endswith("+03:00")
    checkpoint["roots"]["company_facts"]="f"*64
    job.parameters["form"]="changed"
    if capture==2:checkpoint["root_deliveries"]["company_facts"]["checked_at"]="changed"
    assert declaration.parameters==expected
    assert declaration.parameters["form"]=="10-K"


def test_explicit_enqueue_returns_actual_owner_record_and_old_keyword_shape():
    job,checkpoint=inputs()
    actual=replace(job,job_id=99,occurrence_id=100,kind="sec-candidate-stage")
    jobs=ReturnedJobs(actual)
    owned=handler(jobs)
    context=SimpleNamespace(job=job,lease=object())
    request=owned.candidate_stage_request(job,checkpoint)
    assert owned.enqueue_candidate(context,checkpoint) is actual
    lease,kwargs=jobs.calls[0]
    assert lease is context.lease
    assert set(kwargs)=={"child_key","scheduled_for","kind","parameters","due_at","priority","now"}
    assert kwargs["child_key"]==request.schedule_key and kwargs["parameters"]==request.parameters


def test_legacy_private_enqueue_keeps_permissive_existing_double_contract():
    jobs=ReturnedJobs(object())
    context=SimpleNamespace(job=SimpleNamespace(parameters={"legacy":"opaque"},due_at=NOW,priority=2),lease=object())
    checkpoint=dict(cik="legacy-cik",accession="legacy-accession",roots={"legacy-role":"legacy-digest"})
    assert handler(jobs)._enqueue_candidate(context,checkpoint) is jobs.record
    assert jobs.calls[0][1]["parameters"]=={"legacy":"opaque","roots":{"legacy-role":"legacy-digest"}}


@pytest.mark.parametrize("change",[
    {"kind":"other"},{"job_id":True},{"priority":1.0},{"priority":2**31},{"parameters":{"unknown":1}},
])
def test_new_descriptor_rejects_bad_resource_before_any_source_access(change):
    job,checkpoint=inputs()
    with pytest.raises(ValueError):handler().candidate_stage_request(replace(job,**change),checkpoint)


@pytest.mark.parametrize("change",[
    {"capture_revision":True},{"capture_revision":1},{"filing_observation_id":10},{"extra":"unknown"},
])
def test_new_descriptor_rejects_checkpoint_shape_and_identity(change):
    job,checkpoint=inputs();checkpoint.update(change)
    with pytest.raises(ValueError):handler().candidate_stage_request(job,checkpoint)


@pytest.mark.parametrize("value",[True,"A"*64,"a"*65])
def test_invalid_plan_hash_refuses(value):
    job,checkpoint=inputs()
    with pytest.raises(ValueError):handler().candidate_stage_request(job,checkpoint,primary_input_plan_sha256=value)


def test_readonly_missing_orphan_and_exact_return():
    values=lookup_values()
    empty=RowEngine(None)
    assert DurableJobRepository(empty).job_for_occurrence(**values) is None
    orphan=RowEngine({**row(),"job_id":None})
    with pytest.raises(JobIdentityConflict,match="no associated"):DurableJobRepository(orphan).job_for_occurrence(**values)
    actual=row()
    engine=RowEngine(actual)
    result=DurableJobRepository(engine).job_for_occurrence(**values)
    assert result.job_id==73 and result.occurrence_id==42 and result.parameters==values["parameters"]
    assert len(engine.statements)==1 and "FOR UPDATE" not in str(engine.statements[0])


@pytest.mark.parametrize("field,value",[
    ("kind","other"),("due_at",NOW+timedelta(seconds=1)),("priority",31),
    ("max_attempts",4),("deadline_at",NOW+timedelta(hours=1)),
    ("parameters",{"integer":True,"nested":{"checked_at":"2026-09-29T15:00:00+03:00"}}),
    ("parameters",{"integer":1.0,"nested":{"checked_at":"2026-09-29T15:00:00+03:00"}}),
    ("parameters",{"integer":1,"nested":{"checked_at":"2026-09-29T12:00:00+00:00"}}),
])
def test_exact_lookup_refuses_every_immutable_change_including_equal_json_types(field,value):
    actual=row();actual[field]=value
    with pytest.raises(JobIdentityConflict):DurableJobRepository(RowEngine(actual)).job_for_occurrence(**lookup_values())


def test_mutable_lifecycle_diagnostics_do_not_change_immutable_association():
    actual=row()
    actual.update(status="succeeded",attempts=5,ownership_generation=9,owner=None,
        checkpoint={"observed":"later"},error_summary="historical",finished_at=NOW)
    actual["parameters"]={"nested":{"checked_at":"2026-09-29T15:00:00+03:00"},"integer":1}
    result=DurableJobRepository(RowEngine(actual)).job_for_occurrence(**lookup_values())
    assert result.status=="succeeded" and result.ownership_generation==9 and result.checkpoint=={"observed":"later"}


@pytest.mark.parametrize("parameters",[
    {1:"numeric-key","1":"string-key"},{"x":float("nan")},{"x":float("inf")},
    {"x":(1,2)},{"x":object()},{"x":2**63},{"x":"x"*65537},{"x":"😀"*17000},
    {"x":list(range(10001))},
])
def test_new_parameter_admission_refuses_before_sql(parameters):
    with pytest.raises(ValueError):DurableJobRepository(NoAccess()).job_for_occurrence(**{**lookup_values(),"parameters":parameters})


def test_depth_pending_node_and_encoded_envelope_bounds():
    nested={}
    for _ in range(34):nested={"next":nested}
    with pytest.raises(ValueError,match="depth"):_bounded_schedule_parameters(nested)
    many={"x":[{str(i):None for i in range(10000)} for _ in range(7)]}
    with pytest.raises(ValueError,match="node|pending"):_bounded_schedule_parameters(many)
    envelope={str(i):"x"*60000 for i in range(20)}
    with pytest.raises(ValueError,match="1MiB"):_bounded_schedule_parameters(envelope)


def test_large_text_cheap_cap_precedes_strip_or_encoding():
    class Poison(str):
        def strip(self,*args):raise AssertionError("oversized text stripped")
        def encode(self,*args):raise AssertionError("oversized text encoded")
    with pytest.raises(ValueError):_bounded_schedule_text("key",Poison("x"*100000),1024)
    with pytest.raises(ValueError):_bounded_schedule_parameters({"x":Poison("x"*100000)})
    with pytest.raises(ValueError):_bounded_schedule_parameters({Poison("x"*100000):None})


@pytest.mark.parametrize("field",["priority","max_attempts"])
def test_new_lookup_exact_integer_subclass_refuses_before_sql(field):
    class IntSubclass(int):
        pass
    values=lookup_values()
    values[field]=IntSubclass(3)
    with pytest.raises(ValueError,match="exact integers"):
        DurableJobRepository(NoAccess()).job_for_occurrence(**values)
