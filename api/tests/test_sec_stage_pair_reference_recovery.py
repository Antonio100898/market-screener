"""Public reference recovery and real projected SQL boundaries; no database or graph."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.dialects import postgresql

from screener import store
from screener.figure_history import FigureHistoryRepository
from screener.sec_ingestion import SecCandidateStageHandler, SecResourceFetchHandler
from screener.shared_companies import (PairReferenceCapacity, SharedCompanyRepository, _artifact_values,
    _snapshot_metadata_values, _snapshot_sha256, SnapshotArtifact)
from test_sec_completed_capture import Fixture, Forbidden
from test_sec_ingestion import NOW, _job


class Rows:
    def __init__(self, value): self.value = value
    def mappings(self): return self
    def one(self): return self.value
    def one_or_none(self): return self.value
    def __iter__(self): return iter(self.value)


class Projection:
    def __init__(self, fixture):
        self.fixture, self.statements, self.transfers = fixture, [], []
        self.role_count = self.source_count = None
        self.header_size = self.ratio_size = None
        self.drift = False
    def connect(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    @staticmethod
    def sizes(values):
        sizes = [len(value.encode()) if value is not None else 0 for value in values]
        return dict(max_field=max(sizes, default=0), total_bytes=sum(sizes))
    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.statements.append(sql)
        assert not any(name in sql for name in ("canonical_payload", "observation_bindings", "contract_details",
                    "figure_selection", "figure_dependency", "document_evidence", "figure_bundle_coverage"))
        columns = tuple(statement.selected_columns.keys())
        f = self.fixture
        if columns == ("max_field", "total_bytes"):
            if "exact_figure_bundle" in sql:
                value = self.sizes((f.exact["request_key"], f.exact["pipeline_run_key"]))
                if self.header_size is not None: value["max_field"] = self.header_size
            else:
                assert "CAST(company_snapshot.receipt_ratio AS TEXT)" in sql
                value = self.sizes(f.snapshot[name] for name in f.header_text)
                if self.ratio_size is not None: value["max_field"] = self.ratio_size
            return Rows(value)
        if columns == ("count", "max_field", "total_bytes"):
            assert "LEFT OUTER JOIN evidence_artifact" in sql
            value = self.sizes(row[name] for row in f.links for name in f.link_text)
            return Rows(dict(count=len(f.links) if self.role_count is None else self.role_count, **value))
        if "company_snapshot_artifact" in sql:
            assert "LIMIT" in sql
            self.transfers.append("links")
            return Rows(deepcopy(f.links))
        if "figure_bundle_source" in sql:
            assert "LIMIT" in sql
            self.transfers.append("sources")
            return Rows(deepcopy(f.sources))
        if "exact_figure_bundle" in sql:
            self.transfers.append("history")
            return Rows(deepcopy(f.exact))
        self.transfers.append("snapshot")
        value = deepcopy(f.snapshot)
        if self.drift and self.transfers.count("snapshot") > 1:
            value["security_identifier"] += ":changed"
        return Rows(value)
    def scalar(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return len(self.fixture.sources) if self.source_count is None else self.source_count


class PairFixture(Fixture):
    header_text = ("ticker", "exchange_code", "quote_currency", "security_title", "source_accession",
                   "security_basis", "receipt_ratio", "issuer_source", "issuer_identifier", "security_identifier")
    link_text = ("content_sha256", "role", "registration_sha", "object_key", "media_type")
    def __init__(self, *, accession=None):
        super().__init__("sec-resource-fetch-v3")
        self.engine = Projection(self)
        self.companies = SharedCompanyRepository(self.engine)
        self.companies._jobs = self
        self.history = FigureHistoryRepository(self.engine)
        self.pair = SecCandidateStageHandler(edgar=Forbidden(), object_store=self, artifacts=self,
            observations=self, companies=self.companies, history=self.history)
        declaration = self.handler.candidate_stage_request(self.job, self.job.checkpoint)
        self.declaration = declaration
        self.stage = replace(_job(declaration.parameters, kind=declaration.kind), job_id=507, occurrence_id=508,
            status="succeeded", ownership_generation=1, priority=declaration.priority, due_at=declaration.due_at)
        self.links = sorted([dict(content_sha256=row.observation.artifact_sha256, role="sec_root_"+role,
            registration_sha=row.observation.artifact_sha256, object_key="raw/sha256/"+row.observation.artifact_sha256,
            media_type="application/first-registration", byte_size=self.artifact_rows[row.observation.artifact_sha256].byte_size)
            for role in self.job.checkpoint["roots"] for row in [self.row(role)]] +
            [dict(content_sha256="e"*64, role="sec_primary_document", registration_sha="e"*64,
                  object_key="raw/sha256/"+"e"*64, media_type="text/html", byte_size=123)],
            key=lambda value:(value["content_sha256"],value["role"]))
        metadata = dict(ticker="FIX", exchange_code="Nasdaq", quote_currency="USD", security_title=None,
                        source_accession=accession, security_basis="SEC_TICKER_MAPPING_ONLY", receipt_ratio="2.00")
        digest = _snapshot_sha256(store.ENGINE_VERSION, "4"*64, _snapshot_metadata_values(**metadata),
            [_artifact_values(SnapshotArtifact(row["content_sha256"],row["role"])) for row in self.links])
        self.snapshot = dict(snapshot_id=80, security_id=91, engine_revision=store.ENGINE_VERSION, payload_sha256="4"*64,
            snapshot_sha256=digest, issuer_source="SEC", issuer_identifier=self.job.parameters["cik"],
            security_identifier="sec:0000001234:primary-listed-security", **metadata)
        identity = self.pair._engine_identity()
        request = self.pair._request_identity_for_job(self.stage, identity)
        run = "sec-annual:"+request
        checkpoint = dict(checkpoint_revision=2, parameters=deepcopy(self.stage.parameters), request_identity=request,
            engine_revision=store.ENGINE_VERSION, engine_identity_sha256=identity, pipeline_run_key=run, snapshot_id=80,
            snapshot_sha256=digest, security_id=91, exact_bundle_id=81, exact_bundle_sha256="5"*64, snapshot_created=True)
        self.stage = replace(self.stage, checkpoint=checkpoint)
        self.exact = dict(bundle_id=81, request_key=run, pipeline_run_key=run, bundle_sha256="5"*64,
            security_id=91, snapshot_id=80, snapshot_sha256=digest, engine_revision=store.ENGINE_VERSION, contract_revision=2,
            storing_job_id=507, sealed_at=NOW)
        self.sources = sorted((ref["source_item_id"],ref["source_observation_id"],ref["occurrence_id"])
                              for ref in self.job.checkpoint["root_deliveries"].values())
        self.stage_reads, self.stage_drift = 0, None
    def job_for_occurrence(self, **values):
        assert values == vars(self.declaration)
        self.stage_reads += 1
        return deepcopy(self.stage_drift if self.stage_reads > 1 and self.stage_drift is not None else self.stage)
    def recover_pair(self, **changes):
        return self.pair.recover_completed_pair_references(**{**self.identity, **changes})


@pytest.mark.parametrize("accession", [None,"0000001234-26-000001"])
def test_public_pair_reference_success_is_truthful_and_preserves_original_timestamp(accession):
    fixture = PairFixture(accession=accession)
    result = fixture.recover_pair()
    assert result["status"] == "PAIR_REFERENCES_VERIFIED"
    assert result["root_capture_bytes_verified"]
    assert all(result[name] is False for name in ("snapshot_payload_verified", "snapshot_leaf_bytes_verified",
                "exact_graph_verified", "graph_digest_recomputed"))
    assert result["financial_status"] == result["currentness_status"] == "NOT_ASSESSED"
    assert len(result["snapshot"]["artifacts"]) == 5 and result["snapshot"]["header"]["source_accession"] == accession
    assert fixture.events.count("body") == 4
    assert fixture.declaration.parameters["root_deliveries"]["company_facts"]["checked_at"].endswith("+03:00")
    result["snapshot"]["artifacts"].clear()
    assert len(fixture.recover_pair()["snapshot"]["artifacts"]) == 5


@pytest.mark.parametrize("field,value", [("job_id",True),("occurrence_id",1.0),("job_id",2**63),
    ("schedule_key"," x"),("schedule_key","x"*1025),("scheduled_for",NOW.replace(tzinfo=None))])
def test_raw_scalar_refusal_before_owners(field,value):
    owner=SecCandidateStageHandler(edgar=Forbidden(),object_store=Forbidden(),artifacts=Forbidden(),
        observations=Forbidden(),companies=Forbidden(),history=Forbidden())
    with pytest.raises(ValueError): owner.recover_completed_pair_references(**{**Fixture().identity,field:value})


@pytest.mark.parametrize("status",["pending","running","retry_wait","failed"])
def test_stage_status_diagnostics_do_not_read_pair_metadata(status):
    fixture=PairFixture();fixture.stage=replace(fixture.stage,status=status,checkpoint=None)
    result=fixture.recover_pair()
    assert result["status"] == ("STAGE_FAILED" if status=="failed" else "STAGE_NOT_COMPLETED")
    assert fixture.engine.statements == []


def test_absent_stage_and_resource_not_completed_are_distinct():
    fixture=PairFixture();fixture.stage=None
    assert fixture.recover_pair()["status"] == "ABSENT_STAGE_ASSOCIATION"
    fixture=PairFixture();fixture.job=replace(fixture.job,status="pending",checkpoint=None,ownership_generation=0)
    result=fixture.recover_pair()
    assert result["status"] == "CAPTURE_UNAVAILABLE" and result["capture_status"] == "NOT_COMPLETED"
    assert not result["root_capture_bytes_verified"] and fixture.engine.statements == []


@pytest.mark.parametrize("field,value",[("snapshot_id",True),("security_id",91.0),("checkpoint_revision",True),
    ("engine_revision",True),("snapshot_created",1),("snapshot_sha256","a"*65),("exact_bundle_sha256","A"*64),
    ("engine_identity_sha256","0"*64),("request_identity","0"*64),("checkpoint_revision",3)])
def test_actual_succeeded_checkpoint_refuses_aliases_wrong_build_and_legacy(field,value):
    fixture=PairFixture();cp=deepcopy(fixture.stage.checkpoint);cp[field]=value
    fixture.stage=replace(fixture.stage,checkpoint=cp)
    with pytest.raises(ValueError):fixture.recover_pair()
    assert fixture.engine.statements == []


@pytest.mark.parametrize("kind",["snapshot_roles","snapshot_ratio","history_header"])
def test_capacity_precedes_variable_metadata_transfer(kind):
    fixture=PairFixture()
    if kind=="snapshot_roles":fixture.engine.role_count=65
    elif kind=="snapshot_ratio":fixture.engine.ratio_size=4097
    else:fixture.engine.header_size=4097
    result=fixture.recover_pair()
    assert result["status"] == "CAPACITY_UNAVAILABLE" and "snapshot" not in result and "exact" not in result
    assert ("history" not in fixture.engine.transfers)
    if kind!="history_header":assert fixture.engine.transfers == []


@pytest.mark.parametrize("field,value",[("storing_job_id",508),("request_key","wrong"),("contract_revision",3),
    ("snapshot_sha256","8"*64),("engine_revision",190),("bundle_sha256","8"*64)])
def test_history_reference_does_not_borrow_an_unrelated_sealed_pair(field,value):
    fixture=PairFixture();fixture.exact[field]=value
    with pytest.raises(ValueError):fixture.recover_pair()


@pytest.mark.parametrize("change",["count","occurrence","missing_artifact","object_key","role","snapshot_digest"])
def test_complete_source_or_artifact_membership_integrity_refusal(change):
    fixture=PairFixture()
    if change=="count":fixture.engine.source_count=5
    elif change=="occurrence":fixture.sources[0]=(*fixture.sources[0][:2],999)
    elif change=="missing_artifact":fixture.links[-1]["registration_sha"]=None
    elif change=="object_key":fixture.links[-1]["object_key"]="raw/forged"
    elif change=="role":fixture.links[-1]["role"]=" changed"
    else:fixture.snapshot["snapshot_sha256"]="0"*64
    with pytest.raises(ValueError):fixture.recover_pair()


@pytest.mark.parametrize("owner",["resource","stage","snapshot","engine"])
def test_end_fences_detect_observed_drift(owner,monkeypatch):
    fixture=PairFixture()
    if owner=="resource":fixture.drift=replace(fixture.job,ownership_generation=3)
    elif owner=="stage":fixture.stage_drift=replace(fixture.stage,ownership_generation=2)
    elif owner=="snapshot":fixture.engine.drift=True
    else:
        identity=fixture.pair._engine_identity();calls=[]
        def changing():
            calls.append(1);return identity if len(calls)<3 else "0"*64
        monkeypatch.setattr(fixture.pair,"_engine_identity",changing)
    with pytest.raises(ValueError):fixture.recover_pair()


def test_new_raw_numeric_admission_refuses_exponent_before_decimal_factory(monkeypatch):
    fixture=PairFixture();fixture.snapshot["receipt_ratio"]="1e100000000"
    def forbidden(**values):raise AssertionError("invalid SQL text reached Decimal metadata factory")
    monkeypatch.setattr("screener.shared_companies._snapshot_metadata_values",forbidden)
    with pytest.raises(ValueError,match="SQL numeric text"):fixture.recover_pair()


def test_planned_request_and_missing_succeeded_checkpoint_refuse_before_projection():
    fixture=PairFixture();fixture.stage=replace(fixture.stage,checkpoint=None)
    with pytest.raises(ValueError,match="no paired checkpoint"):fixture.recover_pair()
    assert fixture.engine.statements == []
    fixture=PairFixture()
    params={**fixture.stage.parameters,"primary_input_revision":1,"primary_input_plan_sha256":"e"*64}
    fixture.stage=replace(fixture.stage,parameters=params)
    with pytest.raises(ValueError,match="unplanned"):fixture.recover_pair()
    assert fixture.engine.statements == []


@pytest.mark.parametrize("field,value",[("snapshot_id",True),("engine_revision",float(store.ENGINE_VERSION)),("payload_sha256","a"*65)])
def test_projected_snapshot_scalars_are_exact(field,value):
    fixture=PairFixture();fixture.snapshot[field]=value
    with pytest.raises(ValueError):fixture.recover_pair()


def test_registered_metadata_end_drift_refuses_even_when_snapshot_digest_is_unchanged():
    fixture=PairFixture()
    read=fixture.companies.snapshot_reference_on_connection
    calls=[]
    def changing(connection,snapshot_id):
        result=read(connection,snapshot_id);calls.append(1)
        if len(calls)>1:result["artifacts"][-1]["byte_size"]+=1
        return result
    fixture.companies.snapshot_reference_on_connection=changing
    with pytest.raises(ValueError,match="metadata changed"):fixture.recover_pair()


def test_same_engine_requirement_refuses_before_capture():
    fixture=PairFixture();fixture.history.engine=object()
    with pytest.raises(ValueError,match="same-engine"):fixture.recover_pair()
    assert fixture.events == []


def test_end_capacity_change_is_integrity_refusal_not_initial_capacity():
    fixture=PairFixture();read=fixture.companies.snapshot_reference_on_connection;calls=[]
    def changing(connection,snapshot_id):
        calls.append(1)
        if len(calls)>1:raise PairReferenceCapacity("snapshot_roles",65,64)
        return read(connection,snapshot_id)
    fixture.companies.snapshot_reference_on_connection=changing
    with pytest.raises(ValueError,match="metadata changed"):fixture.recover_pair()


@pytest.mark.parametrize("digits", [4095,4096])
def test_normalized_ratio_refuses_carry_beyond_returned_field_limit(digits,monkeypatch):
    fixture=PairFixture();fixture.snapshot["receipt_ratio"]="9"*digits
    metadata={name:fixture.snapshot[name] for name in fixture.header_text[:7]}
    digest=_snapshot_sha256(store.ENGINE_VERSION,fixture.snapshot["payload_sha256"],_snapshot_metadata_values(**metadata),
        [_artifact_values(SnapshotArtifact(row["content_sha256"],row["role"])) for row in fixture.links])
    fixture.snapshot["snapshot_sha256"]=digest;fixture.exact["snapshot_sha256"]=digest
    checkpoint=deepcopy(fixture.stage.checkpoint);checkpoint["snapshot_sha256"]=digest
    fixture.stage=replace(fixture.stage,checkpoint=checkpoint)
    if digits==4096:
        def forbidden(*args,**kwargs):raise AssertionError("oversized normalized text reached reference hash factory")
        monkeypatch.setattr("screener.shared_companies._snapshot_sha256",forbidden)
    result=fixture.recover_pair()
    if digits==4096:
        assert result["status"] == "CAPACITY_UNAVAILABLE" and "snapshot" not in result
        assert result["gap"]["phase"] == "snapshot_returned_field"
    else:
        assert result["status"] == "PAIR_REFERENCES_VERIFIED"
        assert len(result["snapshot"]["header"]["receipt_ratio"].encode()) == 4096


@pytest.mark.parametrize("contract",[1,2,3])
def test_shared_seal_predicate_keeps_old_publication_contract_gate(contract):
    row=dict(security_id=91,snapshot_id=80,pipeline_run_key="run",contract_revision=contract)
    if contract==3:
        with pytest.raises(ValueError,match="contract3"):FigureHistoryRepository._sealed_snapshot_binding(row,security_id=91,snapshot_id=80,expected_run_key="run")
    else:FigureHistoryRepository._sealed_snapshot_binding(row,security_id=91,snapshot_id=80,expected_run_key="run")
