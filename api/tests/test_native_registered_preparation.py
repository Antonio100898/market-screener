"""Scalar actual transport and private proof over synthetic in-memory registry."""
from dataclasses import asdict, replace
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from screener import figure_history as history, primary_input_plan as codec
from screener.captured_assembly import engine_identity
from screener.primary_input_transport import PrimaryInputTransport
from tests.test_figure_history import example_bundle, validate_changed
from test_primary_input_transport import registered
from test_primary_input_plan import wire


class Result:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def __iter__(self): return iter(self.rows)
    def one_or_none(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class RegistryEngine:
    def __init__(self, case): self.case, self.calls = case, []
    def connect(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def scalar(self, statement):
        self.calls.append(str(statement))
        if "alembic_version" in str(statement): return "20261009_0021"
        raise AssertionError("unexpected scalar database access")
    def execute(self, statement):
        self.calls.append(str(statement))
        if "priced_security" in str(statement):
            return Result([dict(security_identifier="synthetic-primary", source_system="SEC", source_identifier=self.case.plan.cik)])
        if "evidence_artifact" in str(statement):
            requested = next(value for value in statement.compile().params.values() if isinstance(value, (list, tuple)))
            return Result([asdict(row) for key, row in self.case.artifacts.rows.items() if key in requested])
        raise AssertionError("unexpected registry query")
    def begin(self): raise AssertionError("refusal reached writer transaction")


@pytest.fixture
def native_case(registered, monkeypatch):
    case = registered
    engine = RegistryEngine(case)
    case.service.observations.engine = engine
    case.engine = engine
    case.plan_sha = case.retain(codec.encode_primary_plan(case.plan))
    from screener import artifacts, source_observations
    monkeypatch.setattr(artifacts, "SqlArtifactRepository", lambda actual: case.artifacts)
    monkeypatch.setattr(source_observations, "SourceObservationRepository", lambda actual: case.service.observations)
    return case


def prepare(case):
    repository = history.FigureHistoryRepository(case.engine)
    proof = repository.prepare_registered_native(case.plan_sha, cik=case.plan.cik,
        company_facts_occurrence_id=case.source.occurrence_id, security_identifier="synthetic-primary",
        expected_engine_identity_sha256=engine_identity(), object_store=case.objects)
    return repository, proof


def source_bundle(proof):
    return history.ExactFigureBundle(security_id=proof.plan.security_id, snapshot_id=1,
        snapshot_sha256="a" * 64, engine_revision=191, pipeline_run_key="source-only",
        contract_revision=3, observations=proof.observations, selections=(), coverage=(),
        sources=proof.sources, document_evidence=proof.document_evidence)


def test_public_scalar_entry_reaches_real_resolve_and_preserves_source_numbers(native_case, monkeypatch):
    calls, reads = [], []
    resolve, read = PrimaryInputTransport.resolve, native_case.objects.read_verified
    def observe(self, *args, **kwargs):
        calls.append(kwargs)
        return resolve(self, *args, **kwargs)
    def read_actual(*args):
        reads.append(args[1]); return read(*args)
    monkeypatch.setattr(PrimaryInputTransport, "resolve", observe)
    monkeypatch.setattr(native_case.objects, "read_verified", read_actual)
    repository, proof = prepare(native_case)
    assert len(calls) == 1 and calls[0]["company_facts_source"] is None
    assert "security_id" not in calls[0] and calls[0]["security_identifier"] == "synthetic-primary"
    assert proof.observations[0].value_decimal_text == "2.06"
    assert proof.observations[0].source_lexical_text == "2.06"
    assert proof.observations[0].reference.location.fact_id is None
    assert proof.observations[0].scope.consolidation == "UNKNOWN"
    assert proof.manifest["native_history"] == "NOT_ASSESSED"
    assert proof.manifest["admission"] == [dict(key="mapping0", status="ADMITTED", reason=None)]
    assert reads.count(proof.observations[0].reference.artifact_sha256) == 2
    assert all(reads.count(d.artifact_sha256) == (2 if d.artifact_sha256 == proof.observations[0].reference.artifact_sha256 else 1)
               for d in proof.plan.dependencies)
    repository._require_storage_contract(source_bundle(proof), proof)
    repository._require_prepared(source_bundle(proof), proof.prepared_documents)


@pytest.mark.parametrize("field,value", [("company_facts_occurrence_id", True), ("company_facts_occurrence_id", 1.0),
    ("company_facts_occurrence_id", 9223372036854775808), ("security_identifier", "x" * 4097),
    ("security_identifier", "é" * 2049), ("cik", "not-a-cik"), ("plan_sha256", True)])
def test_invalid_scalar_inputs_refuse_before_registry_or_object_access(native_case, field, value):
    args = dict(plan_sha256=native_case.plan_sha, cik=native_case.plan.cik,
                company_facts_occurrence_id=3, security_identifier="synthetic-primary")
    args[field] = value
    reads = []
    native_case.objects.read_verified = lambda *args: reads.append(args)
    with pytest.raises(ValueError):
        native_case.service.resolve_registered_native(**args)
    assert not reads and not native_case.engine.calls


@pytest.mark.parametrize("fault", ["record", "manifest", "request", "foreign-key", "document-token"])
def test_constructed_mutated_or_foreign_proofs_refuse_before_writes(native_case, fault):
    repository, proof = prepare(native_case)
    bundle = source_bundle(proof)
    if fault == "record":
        proof = replace(proof, observations=(proof.observations[0].model_copy(update=dict(value_decimal_text="100")),))
    elif fault == "manifest":
        proof = replace(proof, manifest={**proof.manifest, "native_history": "VERIFIED"})
    elif fault == "request":
        proof = replace(proof, company_facts_occurrence_id=4)
    elif fault == "foreign-key":
        repository = history.FigureHistoryRepository(native_case.engine)
    else:
        proof = replace(proof, prepared_documents=replace(proof.prepared_documents, limits_sha256="a" * 64))
    with pytest.raises(ValueError, match="seal|proof"):
        repository.store(None, request_key="tamper", bundle=bundle,
            prepared_native=proof, prepared_documents=proof.prepared_documents)


@pytest.mark.parametrize("fault", ["observation-subset", "document-subset", "source-subset", "reported", "FULL", "unrelated", "action"])
def test_entire_proof_graph_is_source_only_and_full_tuple(native_case, fault):
    repository, proof = prepare(native_case)
    bundle = source_bundle(proof)
    if fault.endswith("subset"):
        field = {"observation-subset": "observations", "document-subset": "document_evidence", "source-subset": "sources"}[fault]
        bundle = validate_changed(bundle, **{field: ()})
    elif fault == "action":
        # Proof-plan legal actions never become graph actions in this operation.
        bundle = validate_changed(bundle, actions=proof.plan.declarations[0].actions)
    else:
        template = example_bundle().selections[0]
        if fault == "reported":
            observed = proof.observations[0]
            node = template.model_copy(update=dict(state="REPORTED", observation_key=observed.key,
                value_decimal_text=observed.value_decimal_text, unit=observed.unit, period=observed.period,
                scope=observed.scope, share_basis=observed.share_basis))
        else:
            node = template.model_copy(update=dict(state="NOT_ASSESSED", value_decimal_text=None,
                observation_key=None, reason="unassessed"))
        coverage = history.Coverage(metric_family=node.metric_family, view=node.view, assessment="PARTIAL",
            metric_paths=(node.metric_path,), reason="unassessed", source_fidelity="EXACT_LEXICAL", earlier_history="CAPTURED_ONLY")
        if fault == "FULL":
            coverage = coverage.model_copy(update=dict(assessment="FULL", reason=None))
            with pytest.raises(ValidationError):
                validate_changed(bundle, selections=(node,), coverage=(coverage,))
            return
        if fault == "unrelated":
            other = example_bundle().observations[0]
            bundle = validate_changed(bundle, observations=(*proof.observations, other))
        bundle = validate_changed(bundle, selections=(node,), coverage=(coverage,))
    with pytest.raises(ValueError, match="full prepared|source-only"):
        repository._require_storage_contract(bundle, proof)


def test_missing_native_delivery_is_real_unavailable_not_a_guessed_number(native_case):
    dependencies = list(native_case.plan.dependencies)
    dependencies[1] = replace(dependencies[1], delivery=None)
    plan = replace(native_case.plan, dependencies=tuple(dependencies))
    native_case.plan_sha = native_case.retain(codec.encode_primary_plan(plan))
    repository, proof = prepare(native_case)
    assert not proof.observations and not proof.document_evidence
    assert proof.manifest["admission"][0] == dict(key="mapping0", status="UNAVAILABLE", reason="NATIVE_REGISTERED_SOURCE_DELIVERY_UNAVAILABLE")
    repository._require_storage_contract(source_bundle(proof), proof)


@pytest.mark.parametrize("fault", ["count", "work", "text", "key", "dependency-record"])
def test_native_raw_budgets_precede_existing_factories(monkeypatch, fault):
    raw = wire()
    if fault == "count": raw["declarations"] *= 33
    elif fault == "work":
        raw["declarations"] *= 9
        for declaration in raw["declarations"]: declaration["original"]["source"]["byte_size"] = 8 * 1024 * 1024
    elif fault == "text": raw["declarations"][0]["key"] = "é" * 2049
    elif fault == "key": raw["declarations"][0]["x" * 4097] = None
    else:
        raw["dependencies"][0]["roles"] = ["company_facts"] * 5000
    monkeypatch.setattr(codec, "_mapping", lambda *args: pytest.fail("budget refusal reached mapping factory"))
    with pytest.raises(codec.PlanCodecError, match="ceiling"):
        codec.decode_native_preparation_plan(json.dumps(raw).encode())


@pytest.mark.parametrize("fault", ["native-body", "shared-CF-role", "aggregate"])
def test_registered_metadata_budget_refuses_before_dependency_reads(native_case, fault):
    plan = native_case.plan
    if fault == "shared-CF-role":
        first = replace(plan.dependencies[0], byte_size=9 * 1024 * 1024, roles=("company_facts", "filing_witness"))
        doc = plan.declarations[0].documents[0].model_copy(update=dict(reference=plan.declarations[0].documents[0].reference.model_copy(
            update=dict(artifact_sha256=first.artifact_sha256))))
        declaration = replace(plan.declarations[0], documents=(*plan.declarations[0].documents, doc.model_copy(update=dict(key="shared-cf-document"))))
        plan = replace(plan, dependencies=(first, *plan.dependencies[1:]), declarations=(declaration,))
        row = native_case.artifacts.rows[first.artifact_sha256]
        native_case.artifacts.rows[first.artifact_sha256] = replace(row, byte_size=first.byte_size)
    elif fault == "native-body":
        second = plan.dependencies[1]
        row = native_case.artifacts.rows[second.artifact_sha256]
        native_case.artifacts.rows[second.artifact_sha256] = replace(row, byte_size=8 * 1024 * 1024 + 1)
    else:
        rows = []
        for index in range(9):
            digest = native_case.retain(f"extra{index}".encode())
            native_case.artifacts.rows[digest] = replace(native_case.artifacts.rows[digest], byte_size=8 * 1024 * 1024)
            rows.append(codec.PlanDependency(digest, 8 * 1024 * 1024, ("unknown",)))
        plan = replace(plan, dependencies=(*plan.dependencies, *rows))
    digest = native_case.retain(codec.encode_primary_plan(plan))
    original, reads = native_case.objects.read_verified, []
    def limited(key, sha, size):
        reads.append(sha)
        if sha != digest: raise AssertionError("metadata rejection reached dependency body")
        return original(key, sha, size)
    native_case.objects.read_verified = limited
    with pytest.raises(ValueError, match="ceiling|size"):
        native_case.service.resolve_registered_native(digest, cik=plan.cik,
            company_facts_occurrence_id=3, security_identifier="synthetic-primary")
    assert reads == [digest]


@pytest.mark.parametrize("fault", [None, "parent-id", "parent-sha"])
def test_explicit_original_parent_revision_is_native_only_context(native_case, fault):
    native = native_case.stored[native_case.plan.dependencies[1].observation_sha256]
    filing = native_case.stored[native_case.plan.dependencies[2].observation_sha256]
    filing.observation.observation_sha256 = native_case.plan.dependencies[2].observation_sha256
    native.item.parent_source_item_id = filing.item.source_item_id
    native.observation.canonical_metadata.update(parent_source_item_id=filing.item.source_item_id,
        parent_observation_id=filing.observation.source_observation_id,
        parent_observation_sha256=filing.observation.observation_sha256)
    if fault == "parent-id": native.observation.canonical_metadata["parent_observation_id"] += 1
    if fault == "parent-sha": native.observation.canonical_metadata["parent_observation_sha256"] = "f" * 64
    # Existing normal resolve still accepts its established item/form/date evidence.
    normal = native_case.service.resolve(native_case.plan_sha, cik=native_case.plan.cik, security_id=91,
        company_facts_source=native_case.source, company_facts_payload=native_case.raw)
    assert normal.manifest["admission"][0]["status"] == "ADMITTED"
    if fault is None:
        assert prepare(native_case)[1].observations
    else:
        with pytest.raises(ValueError, match="parent revision"):
            prepare(native_case)


@pytest.mark.parametrize("name,value", [("parent_source_item_id", True), ("parent_source_item_id", 201.0),
    ("parent_observation_id", True), ("parent_observation_id", 202.0),
    ("parent_observation_id", 9223372036854775808), ("parent_observation_sha256", []),
    ("parent_observation_sha256", "B" * 64)])
def test_parent_context_scalars_are_exact_native_only(native_case, name, value):
    native = native_case.stored[native_case.plan.dependencies[1].observation_sha256]
    filing = native_case.stored[native_case.plan.dependencies[2].observation_sha256]
    native.item.parent_source_item_id = filing.item.source_item_id
    native.observation.canonical_metadata[name] = value
    with pytest.raises(ValueError, match="exact"):
        prepare(native_case)


def test_cf_exemption_needs_actual_registered_role_before_dependency_read(native_case):
    row = native_case.stored[native_case.plan.dependencies[0].observation_sha256]
    row.observation.canonical_metadata["role"] = "primary_document"
    digest = native_case.plan_sha
    old, reads = native_case.objects.read_verified, []
    def only_plan(key, sha, size):
        reads.append(sha)
        if sha != digest: raise AssertionError("unadmitted CF role reached body read")
        return old(key, sha, size)
    native_case.objects.read_verified = only_plan
    with pytest.raises(ValueError, match="identity or role"):
        prepare(native_case)
    assert reads == [digest]


@pytest.mark.parametrize("path", [("security_id",),
    *(('dependencies', 0, 'captured_source', field) for field in ('source_item_id', 'source_observation_id', 'occurrence_id')),
    *(('dependencies', 0, 'delivery', field) for field in ('occurrence_id', 'source_item_id', 'source_observation_id', 'job_id', 'ownership_generation')),
    *(('declarations', 0, 'original', 'source', 'captured_source', field) for field in ('source_item_id', 'source_observation_id', 'occurrence_id')),
    *(('declarations', 0, 'target_origin', 'source', field) for field in ('source_item_id', 'source_observation_id', 'occurrence_id'))])
def test_native_sql_id_overflow_refuses_before_any_factories(monkeypatch, path):
    raw = wire()
    record = raw
    for step in path[:-1]: record = record[step]
    record[path[-1]] = 2**63
    monkeypatch.setattr(codec, "_mapping", lambda *args: pytest.fail("SQL overflow reached mapping factory"))
    monkeypatch.setattr(codec, "_dependency", lambda *args: pytest.fail("SQL overflow reached dependency factory"))
    with pytest.raises(ValueError, match="signed64"):
        codec.decode_native_preparation_plan(json.dumps(raw).encode())


def test_native_delivery_null_refuses_before_factory_but_captured_null_remains_unavailable(monkeypatch):
    raw = wire()
    raw['dependencies'][0]['captured_source']['occurrence_id'] = None
    raw['dependencies'][0]['delivery'] = None
    assert codec.decode_native_preparation_plan(json.dumps(raw).encode()).dependencies[0].captured_source.occurrence_id is None
    raw = wire()
    raw['dependencies'][0]['delivery']['occurrence_id'] = None
    monkeypatch.setattr(codec, "_mapping", lambda *args: pytest.fail("null delivery reached mapping factory"))
    with pytest.raises(ValueError, match="SQL identifier"):
        codec.decode_native_preparation_plan(json.dumps(raw).encode())


def test_native_signed64_header_max_and_legacy_unbounded_header_neighbor():
    raw = wire(); raw['security_id'] = 2**63 - 1
    assert codec.decode_native_preparation_plan(json.dumps(raw).encode()).security_id == 2**63 - 1
    raw['security_id'] += 1
    assert codec.decode_primary_plan(json.dumps(raw).encode()).security_id == 2**63


@pytest.mark.parametrize("roles", [("company_facts", "filing_witness"), ("company_facts", "unknown")])
def test_native_cf_additional_usage_caps_before_dependency_read(native_case, roles):
    dependency = replace(native_case.plan.dependencies[0], byte_size=9 * 1024 * 1024, roles=roles)
    native_case.artifacts.rows[dependency.artifact_sha256] = replace(native_case.artifacts.rows[dependency.artifact_sha256], byte_size=dependency.byte_size)
    plan = replace(native_case.plan, dependencies=(dependency, *native_case.plan.dependencies[1:]))
    digest = native_case.retain(codec.encode_primary_plan(plan))
    original, reads = native_case.objects.read_verified, []
    def only_plan(key, sha, size):
        reads.append(sha)
        if sha != digest: pytest.fail("shared CF usage exceeds8MiB reached dependency body")
        return original(key, sha, size)
    native_case.objects.read_verified = only_plan
    with pytest.raises(ValueError, match="ceiling"):
        native_case.service.resolve_registered_native(digest, cik=plan.cik,
            company_facts_occurrence_id=native_case.source.occurrence_id, security_identifier="synthetic-primary")
    assert reads == [digest]


@pytest.mark.parametrize("roles,large", [(("company_facts",), True), (("target",), True),
    (("company_facts", "target"), True), (("company_facts", "unknown"), False)])
def test_native_cf_known_only_large_and_unknown_small_neighbors(native_case, roles, large):
    first = native_case.plan.dependencies[0]
    if large:
        native_case.raw = native_case.raw[:-1] + b',"synthetic_padding":"' + b'x' * (9 * 1024 * 1024) + b'"}'
        digest = native_case.retain(native_case.raw)
        captured = replace(first.captured_source, artifact_sha256=digest)
        native_case.stored[first.observation_sha256].observation.artifact_sha256 = digest
        target = replace(native_case.plan.declarations[0].target_origin, source=captured)
        declaration = replace(native_case.plan.declarations[0], target_origin=target)
        first = replace(first, captured_source=captured, artifact_sha256=digest, byte_size=len(native_case.raw))
        native_case.plan = replace(native_case.plan, declarations=(declaration,))
        native_case.source = captured
    first = replace(first, roles=roles)
    native_case.plan = replace(native_case.plan, dependencies=(first, *native_case.plan.dependencies[1:]))
    native_case.plan_sha = native_case.retain(codec.encode_primary_plan(native_case.plan))
    repository, proof = prepare(native_case)
    assert proof.manifest['admission'][0]['status'] == 'ADMITTED'
    assert proof.plan.dependencies[0].roles == roles
    assert proof.observations[0].value_decimal_text == '2.06'
