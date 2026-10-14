"""Registered scalar reconstruction through actual transport/derive; pure registry."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from screener import captured_assembly, exact_annual, figure_history as history, sync
from screener.company_import import _canonical_json
from screener.shared_companies import SnapshotArtifact
from test_primary_input_transport import registered
from test_primary_input_transport_callers import planned_preparation
from test_native_registered_preparation import RegistryEngine


@pytest.fixture
def reconstruction(planned_preparation, monkeypatch):
    fixture = planned_preparation
    case = fixture.case
    engine = RegistryEngine(case)
    case.service.observations.engine = engine
    fixture.company.security_identifier = "synthetic-primary"
    # The SQL JSONB owner returns string object keys, including annual years.
    fixture.company.snapshot.canonical_payload = json.loads(_canonical_json(fixture.company.snapshot.canonical_payload))
    from screener import artifacts, shared_companies, source_observations
    monkeypatch.setattr(artifacts,"SqlArtifactRepository",lambda actual:case.artifacts)
    monkeypatch.setattr(source_observations,"SourceObservationRepository",lambda actual:case.service.observations)
    monkeypatch.setattr(shared_companies,"SharedCompanyRepository",lambda actual:SimpleNamespace(
        snapshot=lambda sid:fixture.company if sid==1 else None))
    fixture.repository = history.FigureHistoryRepository(engine)
    fixture.recipe_sha = case.retain(_canonical_json(fixture.recipe))
    return fixture


def prepare(fixture):
    return fixture.repository.prepare_registered_native_reconstruction(fixture.recipe_sha,
        expected_engine_identity_sha256=captured_assembly.engine_identity(),object_store=fixture.case.objects)


def test_public_scalar_actual_two_view_reconstruction_has_unqualified_source_support(reconstruction,monkeypatch):
    f=reconstruction
    derive,build=sync._derive_evidence_result,sync.build_snapshot
    calls,views=[],[]
    def derive_actual(bundle,*args,**kwargs):calls.append(bundle);return derive(bundle,*args,**kwargs)
    def build_actual(*args,**kwargs):views.append(kwargs["assume_absent_zero"]);return build(*args,**kwargs)
    monkeypatch.setattr(sync,"_derive_evidence_result",derive_actual)
    monkeypatch.setattr(sync,"build_snapshot",build_actual)
    monkeypatch.setattr(f.case.objects,"put_verified",lambda *a:pytest.fail("readonly reconstruction wrote"))
    proof=prepare(f)
    assert len(calls)==1 and views==[False,True]
    payload=f.repository.verify_native_reconstruction(proof)
    assert payload["canonical_row"]==json.loads(_canonical_json(f.company.snapshot.canonical_payload))
    assert payload["strict_status"]==payload["assumed_status"]=="ok"
    assert payload["retrospective_run"] and payload["financial_status"]==payload["currentness_status"]=="NOT_ASSESSED"
    assert proof.selections and all(n.state=="NOT_ASSESSED" and n.value_decimal_text is None for n in proof.selections)
    assert all(w.association_status=="DECLARED_NOT_VERIFIED" for n in proof.selections for w in n.source_witnesses)
    assert all(c.assessment=="PARTIAL" for c in proof.coverage)
    assert type(proof) is history.PreparedNativeReconstruction and not isinstance(proof,history.PreparedRegisteredNative)
    payload["canonical_row"]["ticker"]="forged"
    assert f.repository.verify_native_reconstruction(proof)["canonical_row"]["ticker"]=="TEST"


def test_private_rejected_decision_support_is_not_published_as_an_operand(reconstruction, monkeypatch):
    from screener import normalize
    f = reconstruction
    original = normalize._split_factor
    marker = object()
    seen = []
    def collect(*args, **kwargs):
        result = original(*args, **kwargs)
        support = kwargs.get('decision_support')
        if support is not None:
            support.append(dict(phase='rejected-source-private-test', source=marker))
            seen.append(marker)
        return result
    monkeypatch.setattr(normalize, '_split_factor', collect)
    proof = prepare(f)
    payload = f.repository.verify_native_reconstruction(proof)
    assert payload['financial_status'] == payload['currentness_status'] == 'NOT_ASSESSED'
    assert all(node.value_decimal_text is None for node in proof.selections)
    assert all(w.association_status == 'DECLARED_NOT_VERIFIED' for node in proof.selections for w in node.source_witnesses)
    assert seen
    assert 'rejected-source-private-test' not in json.dumps(payload)


@pytest.mark.parametrize("change",["many_members","bool_id","float_id","wide_id","market","revision","old_build"])
def test_raw_recipe_refusal_before_snapshot_factories(reconstruction,monkeypatch,change):
    f=reconstruction
    recipe=deepcopy(f.recipe)
    if change=="many_members":recipe["members"]*=2
    elif change=="bool_id":recipe["members"][0]["snapshot_id"]=True
    elif change=="float_id":recipe["members"][0]["snapshot_id"]=1.0
    elif change=="wide_id":recipe["members"][0]["snapshot_id"]=2**63
    elif change=="market":recipe["members"][0]["market_recipe_sha256"]="a"*64
    elif change=="revision":recipe["recipe_revision"]=True
    else:recipe["engine_identity_sha256"]="a"*64
    f.recipe_sha=f.case.retain(_canonical_json(recipe))
    monkeypatch.setattr(captured_assembly.CapturedCanonicalPreparer,"_bundle",lambda *a,**kw:pytest.fail("invalid recipe reached bundle"))
    with pytest.raises((ValueError,captured_assembly.PreparationUnavailable)):prepare(f)


@pytest.mark.parametrize("failure",["other_security","other_plan","ambiguous_cf","late_size","shared_cf","too_many"])
def test_snapshot_and_union_refusal_before_any_dependency_body(reconstruction,monkeypatch,failure):
    f=reconstruction
    links=list(f.company.artifacts)
    if failure=="other_security":f.company.snapshot.security_id+=1
    elif failure=="other_plan":links.append(SnapshotArtifact("a"*64,"primary_input_plan"))
    elif failure=="ambiguous_cf":links.append(SnapshotArtifact("a"*64,"sec_root_company_facts"))
    elif failure=="too_many":
        for i in range(32):links.append(SnapshotArtifact(f.case.retain(str(i).encode()),"extra"+str(i)))
    elif failure=="late_size":
        digest=f.case.retain(b"late")
        f.case.artifacts.rows[digest]=replace(f.case.artifacts.rows[digest],byte_size=8*1024*1024+1)
        links.append(SnapshotArtifact(digest,"extra-last"))
    else:
        digest=links[0].content_sha256
        f.case.artifacts.rows[digest]=replace(f.case.artifacts.rows[digest],byte_size=9*1024*1024)
        links.append(SnapshotArtifact(digest,"extra-use"))
    f.company.artifacts=tuple(links)
    allowed={f.recipe_sha,f.recipe["members"][0]["primary_input_plan_sha256"]}
    actual=f.case.objects.read_verified
    def read(key,digest,size):
        assert digest in allowed,"bad union read dependency before frontier"
        return actual(key,digest,size)
    monkeypatch.setattr(f.case.objects,"read_verified",read)
    with pytest.raises((ValueError,captured_assembly.PreparationUnavailable)):prepare(f)


def test_whole_canonical_row_mismatch_refuses(reconstruction):
    f=reconstruction
    f.company.snapshot.canonical_payload={**f.company.snapshot.canonical_payload,"ticker":"different"}
    f.company.snapshot.payload_sha256=hashlib.sha256(_canonical_json(f.company.snapshot.canonical_payload)).hexdigest()
    with pytest.raises(captured_assembly.PreparationUnavailable,match="reproduce"):prepare(f)


def test_counterfeit_proof_and_old_writer_admission_remain_closed(reconstruction):
    f=reconstruction
    proof=prepare(f)
    with pytest.raises(ValueError):f.repository.verify_native_reconstruction(replace(proof,payload=proof.payload+b" "))
    with pytest.raises(ValueError):f.repository.verify_native_reconstruction(replace(proof,selections=()))
    from test_native_registered_preparation import source_bundle
    unrelated=SimpleNamespace(plan=f.plan,observations=proof.observations,document_evidence=proof.document_evidence,
                              sources=proof.sources)
    with pytest.raises(ValueError,match="registered native preparation unavailable"):
        f.repository._require_storage_contract(source_bundle(unrelated),proof)


def test_supplied_result_or_cache_cannot_enter_scalar_operation(reconstruction):
    f=reconstruction
    with pytest.raises(TypeError):f.repository.prepare_registered_native_reconstruction(f.recipe_sha,
        expected_engine_identity_sha256=captured_assembly.engine_identity(),object_store=f.case.objects,result=object())


def test_final_referenced_snapshot_change_refuses(reconstruction,monkeypatch):
    f=reconstruction
    derive=sync._derive_evidence_result
    def actual_then_change(*args,**kwargs):
        result=derive(*args,**kwargs)
        f.company.snapshot.snapshot_sha256="e"*64
        return result
    monkeypatch.setattr(sync,"_derive_evidence_result",actual_then_change)
    with pytest.raises(ValueError,match="graph changed"):prepare(f)


@pytest.mark.parametrize("shape",["cycle","depth","edges","outputs","unavailable"])
def test_public_actual_run_graph_frontier_and_unavailable_roots(reconstruction,monkeypatch,shape):
    from screener.models import UnavailablePrimaryInput
    from screener.normalize import _SelectedAnnualSeries
    f=reconstruction
    derive=sync._derive_evidence_result
    def actual_with_graph(*args,**kwargs):
        result=derive(*args,**kwargs)
        series=result.snapshot.annual_eps
        year,fact=next(iter(series.items()))
        if shape=="cycle":object.__setattr__(fact,"operation_inputs",(fact,))
        elif shape=="depth":
            child=fact
            for _ in range(34):child=replace(fact,operation_inputs=(child,))
            series[year]=child
        elif shape=="edges":series[year]=replace(fact,operation_inputs=(fact,)*8193)
        elif shape=="outputs":result=replace(result,snapshot=replace(result.snapshot,annual_eps={i:fact for i in range(1,130)}))
        else:
            mapping=f.plan.declarations[0]
            reason=UnavailablePrimaryInput(mapping.key,mapping.canonical_namespace,mapping.canonical_tag,
                "KNOWN_RETAINED_UNAVAILABLE",mapping.original,mapping.target_origin)
            result=replace(result,snapshot=replace(result.snapshot,annual_eps=_SelectedAnnualSeries(series,{2030:reason})))
        return result
    monkeypatch.setattr(sync,"_derive_evidence_result",actual_with_graph)
    if shape=="unavailable":
        proof=prepare(f)
        node=next(n for n in proof.selections if n.view=="STRICT" and n.metric_path=="annual_eps.2030")
        assert node.value_decimal_text is None and node.reason=="KNOWN_RETAINED_UNAVAILABLE"
        assert all(w.role!="ORIGINAL_NATIVE" for w in node.source_witnesses)
    else:
        with pytest.raises(ValueError):prepare(f)


def test_public_actual_derived_eps_uses_its_operand_objects_and_preserves_absent_reason(reconstruction,monkeypatch):
    from screener.models import DerivedEpsInputs
    f=reconstruction
    derive=sync._derive_evidence_result
    def with_operands(*args,**kwargs):
        result=derive(*args,**kwargs)
        year=next(iter(result.snapshot.annual_eps))
        income=result.snapshot.annual_eps[year]
        count=next(iter(result.snapshot.annual_eps.values()))
        result.snapshot.annual_eps[year]=replace(next(iter(result.snapshot.annual_eps.values())),primary_input=None,
            exact_origin=None,derived_eps_inputs=DerivedEpsInputs(income,count,None,"ABSENCE_NOT_VERIFIED"),operation_inputs=())
        return result
    monkeypatch.setattr(sync,"_derive_evidence_result",with_operands)
    proof=prepare(f)
    value=f.repository.verify_native_reconstruction(proof)
    assert value["projection"]["operand_gaps"]
    assert any(w.role=="OPERAND_SOURCE" for n in proof.selections for w in n.source_witnesses)


def test_unused_admitted_equal_amount_is_not_grafted_into_output(reconstruction):
    f=reconstruction
    proof=prepare(f)
    assert f.repository.verify_native_reconstruction(proof)["manifest"]["admission"][0]["status"]=="ADMITTED"
    assert not any(w.role=="ORIGINAL_NATIVE" for n in proof.selections for w in n.source_witnesses)


def test_selected_native_target_has_original_cf_and_native_distinct_support(reconstruction):
    from screener.primary_input_plan import encode_primary_plan
    from screener.sources.sec_json import decode_company_facts, ORIGIN_KEY
    from screener.evidence import EvidenceBundle
    f=reconstruction
    old=f.plan.dependencies[0]
    raw=f.case.objects.read_verified("raw/sha256/"+old.artifact_sha256,old.artifact_sha256,old.byte_size)
    value=json.loads(raw)
    entries=value["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
    target=f.plan.declarations[0].target_origin
    index=int(target.pointer.rsplit("/",2)[1])
    entries[:]=[entry for i,entry in enumerate(entries) if i==index or entry.get("end")!=target.end]
    raw=json.dumps(value).encode()
    digest=f.case.retain(raw)
    source=replace(old.captured_source,artifact_sha256=digest)
    tree=decode_company_facts(raw,source=source)
    origin=next(entry[ORIGIN_KEY] for entry in tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
                    if entry.get("end")==target.end)
    # The legacy transport fixture is USD, so it is admitted but not an EPS consumer.
    from test_native_primary_inputs import document
    old_native=f.plan.dependencies[1]
    native_raw=f.case.objects.read_verified("raw/sha256/"+old_native.artifact_sha256,old_native.artifact_sha256,old_native.byte_size)
    native_raw=native_raw.replace(b'<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>',
        b'<x:unit id="u"><x:divide><x:unitNumerator><x:measure>iso:USD</x:measure></x:unitNumerator>'
        b'<x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit>')
    native_sha=f.case.retain(native_raw)
    native_capture=replace(old_native.captured_source,artifact_sha256=native_sha)
    declaration=f.plan.declarations[0]
    request=replace(declaration.original,source=replace(declaration.original.source,artifact_sha256=native_sha,
        byte_size=len(native_raw),captured_source=native_capture),unit_denominator=('{http://www.xbrl.org/2003/instance}shares',))
    old_doc=declaration.documents[0]
    new_doc=document(native_raw,request,key=old_doc.key,roles=old_doc.roles)
    new_doc=new_doc.model_copy(update={'reference':new_doc.reference.model_copy(update={
        'source_published_at':old_doc.reference.source_published_at})})
    f.plan=replace(f.plan,dependencies=(replace(old,artifact_sha256=digest,byte_size=len(raw),captured_source=source),
        replace(old_native,artifact_sha256=native_sha,byte_size=len(native_raw),captured_source=native_capture),*f.plan.dependencies[2:]),
        declarations=(replace(declaration,original=request,target_origin=origin,documents=(new_doc,)),))
    f.case.stored[old.observation_sha256].observation.artifact_sha256=digest
    f.case.stored[old_native.observation_sha256].observation.artifact_sha256=native_sha
    plan_sha=f.case.retain(encode_primary_plan(f.plan))
    resolved=f.case.service.resolve(plan_sha,cik=f.plan.cik,security_id=f.plan.security_id,
        company_facts_source=source,company_facts_payload=raw)
    actual=sync._derive_evidence_result(EvidenceBundle(f.plan.cik,"TEST",resolved.original_company_facts,None,None,
        "missing",resolved.captured_sources,resolved.original_company_facts,resolved.outcomes))
    assert actual.status=="ok"
    f.company.snapshot.canonical_payload=json.loads(_canonical_json(actual.row))
    f.company.snapshot.payload_sha256=hashlib.sha256(_canonical_json(actual.row)).hexdigest()
    f.company.artifacts=tuple(SnapshotArtifact(digest if link.content_sha256==old.artifact_sha256 else
        native_sha if link.content_sha256==old_native.artifact_sha256 else
        plan_sha if link.role=="primary_input_plan" else link.content_sha256,link.role) for link in f.company.artifacts)
    recipe=deepcopy(f.recipe);recipe["members"][0]["primary_input_plan_sha256"]=plan_sha
    f.recipe_sha=f.case.retain(_canonical_json(recipe))
    proof=prepare(f)
    native_nodes=[node for node in proof.selections if any(w.role=="ORIGINAL_NATIVE" for w in node.source_witnesses)]
    assert native_nodes
    assert all({"ORIGINAL_COMPANY_FACTS","ORIGINAL_NATIVE"}<={w.role for w in node.source_witnesses} for node in native_nodes)
    assert all(node.value_decimal_text is None for node in native_nodes)


def test_executing_build_refusal_before_schema_or_body(reconstruction,monkeypatch):
    f=reconstruction
    monkeypatch.setattr(history,"_native_reconstruction_identity",lambda:"f"*64)
    with pytest.raises(ValueError,match="identity differs"):prepare(f)


@pytest.mark.parametrize("field,value",[("engine_revision",True),("engine_revision",1.0),
    ("engine_identity_sha256",True),("engine_identity_sha256","a"*65),
    ("primary_input_plan_sha256",True),("primary_input_plan_sha256","a"*65),
    ("source_observation_sha256",[True]),("source_observation_sha256",["a"*65])],
    ids=["engine-bool","engine-float","engine-sha-bool","engine-sha-wide","plan-bool","plan-wide","source-bool","source-wide"])
def test_every_new_raw_primitive_refuses_before_ordinary_recipe_factory(reconstruction,monkeypatch,field,value):
    f=reconstruction
    recipe=deepcopy(f.recipe)
    if field in {"engine_revision","engine_identity_sha256"}:recipe[field]=value
    else:recipe["members"][0][field]=value
    f.recipe_sha=f.case.retain(_canonical_json(recipe))
    monkeypatch.setattr(captured_assembly,"_recipe",lambda *a:pytest.fail("invalid raw value reached ordinary recipe factory"))
    with pytest.raises(ValueError):prepare(f)


class IntegerSubclass(int):pass
class TextSubclass(str):pass


@pytest.mark.parametrize("field,value",[("snapshot_id",True),("snapshot_id",1.0),("snapshot_id",IntegerSubclass(1)),
    ("security_id",True),("security_id",91.0),("security_id",IntegerSubclass(91)),
    ("recipe_sha256",TextSubclass("a"*64)),("engine_identity_sha256","a"*65),
    ("schema_revision",TextSubclass("20261009_0021")),("schema_revision",True)],
    ids=["snapshot-bool","snapshot-float","snapshot-subclass","security-bool","security-float","security-subclass",
         "recipe-subclass","engine-wide","schema-subclass","schema-bool"])
def test_real_seal_cannot_authorize_equal_but_untyped_headers(reconstruction,field,value):
    f=reconstruction
    proof=prepare(f)
    with pytest.raises(ValueError):f.repository.verify_native_reconstruction(replace(proof,**{field:value}))
