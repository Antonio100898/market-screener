"""Security-class support proves original member bytes, never a numeric fact."""
import hashlib
import io
import zipfile
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from screener import figure_history as owner
from screener.artifacts import store_evidence
from screener.object_store import ImmutableObjectStore
from tests.test_artifacts import MemoryRepository
from tests.test_figure_history import example_bundle, validate_changed
from tests.test_figure_history_contract2 import basis_graph, document_bundle
from tests.test_figure_history_contract3 import MetadataEngine
from tests.test_object_store import MemoryS3


MEMBER_NAME = "XBRL/PublicDoc/issuer.xbrl"
MEMBER = '''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor">
 <jpcrp_cor:IssuedSharesTotalNumberOfSharesEtcTextBlock contextRef="filing">
 &lt;table&gt;&lt;tr&gt;&lt;td&gt;普通株式&lt;/td&gt;&lt;td&gt;38,888,000&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;
 </jpcrp_cor:IssuedSharesTotalNumberOfSharesEtcTextBlock></xbrli:xbrl>'''.encode("utf-8")


def archive_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MEMBER_NAME, MEMBER)
    return stream.getvalue()


def class_document(member_digest):
    return owner.DocumentEvidence(key="issuer-classes", roles=("SECURITY_CLASS",),
        reference=owner.SourceOccurrenceRef(artifact_sha256=member_digest,
            extraction_revision="edinet-security-class-document-v1", source_system="EDINET",
            issuer_source_identifier="E03475", source_document="S100Z2YO/" + MEMBER_NAME,
            fidelity="EXACT_LEXICAL", location=owner.DocumentSpan(start_byte=0,
                end_byte=len(MEMBER), span_sha256=hashlib.sha256(MEMBER).hexdigest())))


def test_registered_original_member_support_prepares_without_numeric_admission():
    objects = ImmutableObjectStore(MemoryS3(), "evidence")
    artifacts = MemoryRepository()
    # Use the production registration owner on actual archive/member bytes.
    archive = archive_bytes()
    archive_artifact = store_evidence(archive, "application/zip", objects, artifacts)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = bundle.read(MEMBER_NAME)
    assert member == MEMBER
    member_artifact = store_evidence(member, "application/xml", objects, artifacts)
    assert member_artifact.content_sha256 != archive_artifact.content_sha256
    document = class_document(member_artifact.content_sha256)
    repository = owner.FigureHistoryRepository(MetadataEngine([asdict(member_artifact)]))
    token = repository.prepare_document_records((document,), objects, contract_revision=3)
    assert isinstance(token, owner.PreparedContract3DocumentEvidence)
    assert token.record_sha256 == owner._digest([document.model_dump(mode="json")])
    observed = example_bundle().observations[0]
    with pytest.raises(ValidationError, match="nonnumeric document evidence"):
        owner.ReportedObservation.model_validate({**observed.model_dump(), "reference": document.reference})


def test_unregistered_member_cannot_borrow_archive_registration():
    objects = ImmutableObjectStore(MemoryS3(), "evidence")
    archive_artifact = store_evidence(archive_bytes(), "application/zip", objects, MemoryRepository())
    document = class_document(hashlib.sha256(MEMBER).hexdigest())
    repository = owner.FigureHistoryRepository(MetadataEngine([asdict(archive_artifact)]))
    with pytest.raises(ValueError, match="not registered verified evidence"):
        repository.prepare_document_records((document,), objects, contract_revision=3)
    # Even registered archive bytes cannot authenticate a member-byte locator.
    wrong = document.model_copy(update={"reference": document.reference.model_copy(
        update={"artifact_sha256": archive_artifact.content_sha256})})
    with pytest.raises(ValueError, match="byte offset/span hash"):
        repository.prepare_document_records((wrong,), objects, contract_revision=3)


@pytest.mark.parametrize("fault", ["span", "offset", "encoding"])
def test_original_member_byte_boundaries_are_enforced(fault):
    objects = ImmutableObjectStore(MemoryS3(), "evidence")
    artifact = store_evidence(MEMBER, "application/xml", objects, MemoryRepository())
    document = class_document(artifact.content_sha256)
    updates = {"span": {"span_sha256": "0" * 64},
               "offset": {"end_byte": len(MEMBER) + 1}, "encoding": {"encoding": "ascii"}}[fault]
    span = document.reference.location.model_copy(update=updates)
    document = document.model_copy(update={"reference": document.reference.model_copy(update={"location": span})})
    repository = owner.FigureHistoryRepository(MetadataEngine([asdict(artifact)]))
    with pytest.raises((ValueError, UnicodeDecodeError)):
        repository.prepare_document_records((document,), objects, contract_revision=3)


def test_security_class_role_cannot_authorize_an_incorporated_basis_or_legal_action():
    bundle = document_bundle()
    document = bundle.document_evidence[0].model_copy(update={"roles": ("SECURITY_CLASS",)})
    bundle = validate_changed(bundle, document_evidence=(document,))
    with pytest.raises(ValidationError, match="required evidence role"):
        basis_graph(bundle)
