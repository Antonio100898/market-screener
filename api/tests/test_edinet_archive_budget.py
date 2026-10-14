"""Bound retained EDINET acquisition and total CRC work before registration."""
import io
from pathlib import Path
import zipfile

import pytest

from screener import company_import
from screener.company_availability import validate_unavailable_row
from screener.company_import import RetainedInputCeiling, RetainedInputError
from screener.sources import edinet
from test_company_import import retained, _edinet, _importer, _sec


def _archive(*, attachment=b"payload", duplicate=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("XBRL/PublicDoc/report.xbrl", "<xbrl/>")
        bundle.writestr("attachments/report.pdf", attachment)
        if duplicate:
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr("attachments/report.pdf", b"other")
    return output.getvalue()


def test_unrelated_member_expansion_is_refused_before_crc(monkeypatch):
    source = _archive(attachment=b"a" * 4096)
    monkeypatch.setattr(edinet, "_ARCHIVE_EXPANSION_LIMIT", 4096)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: pytest.fail("ceiling must precede CRC"))
    with pytest.raises(edinet.EdinetInputCeilingError, match="aggregate source ceiling"):
        edinet.validate_xbrl_archive(source, "S100TEST")


def test_duplicate_names_are_errors_not_supported_input_ceilings(monkeypatch):
    source = _archive(duplicate=True)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: pytest.fail("ambiguity must precede CRC"))
    with pytest.raises(edinet.EdinetError, match="duplicate") as error:
        edinet.validate_xbrl_archive(source, "S100TEST")
    assert not isinstance(error.value, edinet.EdinetInputCeilingError)


def test_member_count_is_refused_before_crc(monkeypatch):
    source = _archive()
    monkeypatch.setattr(edinet, "_ARCHIVE_MEMBER_LIMIT", 1)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: pytest.fail("count must precede CRC"))
    with pytest.raises(edinet.EdinetInputCeilingError, match="member source ceiling"):
        edinet.validate_xbrl_archive(source, "S100TEST")


def test_below_ceiling_archive_still_checks_all_member_crc():
    edinet.validate_xbrl_archive(_archive(), "S100TEST")
    output = io.BytesIO()
    payload = b"known attachment CRC"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("XBRL/PublicDoc/report.xbrl", "<xbrl/>")
        archive.writestr("attachments/report.pdf", payload)
    damaged = bytearray(output.getvalue())
    damaged[damaged.index(payload)] ^= 1
    with pytest.raises(edinet.EdinetError, match="corrupt archive member") as error:
        edinet.validate_xbrl_archive(bytes(damaged), "S100TEST")
    assert not isinstance(error.value, edinet.EdinetInputCeilingError)


def test_retained_read_is_bounded_before_validation_or_zip_retention(retained, monkeypatch):
    _edinet(*retained)
    _, cache = retained
    original = (cache / "edinet/S100OLD.zip").read_bytes()
    limit = len(original) - 1
    monkeypatch.setattr(edinet, "_XBRL_INPUT_LIMIT", limit)
    reads = []
    original_open = Path.open

    class BoundedReader:
        def __init__(self, source):
            self.source = source
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def read(self, amount):
            reads.append(amount)
            assert amount > 0 and amount <= limit + 1
            return self.source.read(amount)

    def tracked_open(path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        return BoundedReader(source) if path.suffix == ".zip" else source

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(company_import, "validate_xbrl_archive", lambda *_: pytest.fail("read ceiling must precede CRC"))
    importer, objects, _, companies = _importer(retained, lambda _: pytest.fail("unsupported source cannot derive"))
    result = importer.import_ticker("6752.T", preserve_unavailable=True)
    assessment, _ = validate_unavailable_row(companies.calls[-1]["canonical_payload"])
    assert result.data_status == "UNAVAILABLE"
    assert assessment.reason_code == "NORMALIZATION_UNSUPPORTED"
    assert assessment.kind == "LOCAL_RETAINED_INPUT_ASSESSMENT"
    assert not assessment.remote_observation_ids
    assert "not fully validated" in assessment.reason and "not checked" in assessment.reason
    assert reads == [limit + 1]
    assert not any(body.startswith(b"PK") for body in objects.data.values())


def test_ceiling_default_mode_raises_with_source_cause(retained, monkeypatch):
    _edinet(*retained)
    monkeypatch.setattr(edinet, "_XBRL_INPUT_LIMIT", 1)
    importer, _, _, companies = _importer(retained, lambda _: pytest.fail("ceiling cannot derive"))
    with pytest.raises(RetainedInputCeiling) as error:
        importer.import_ticker("6752.T")
    assert isinstance(error.value.__cause__, edinet.EdinetInputCeilingError)
    assert not companies.calls


@pytest.mark.parametrize("failure", [edinet.EdinetError("ambiguous source"), MemoryError("memory"), RuntimeError("programming")])
def test_rebuild_errors_never_become_unavailable_success(retained, monkeypatch, failure):
    _edinet(*retained)
    def fail(*args):
        raise failure
    monkeypatch.setattr(company_import, "rebuild_companyfacts", fail)
    importer, _, _, companies = _importer(retained, lambda _: pytest.fail("invalid source cannot derive"))
    with pytest.raises(RetainedInputError if isinstance(failure, edinet.EdinetError) else type(failure)) as error:
        importer.import_ticker("6752.T", preserve_unavailable=True)
    if isinstance(failure, edinet.EdinetError):
        assert error.value.__cause__ is failure
    assert not companies.calls


def test_malformed_archive_is_not_retained_or_green_unavailable(retained):
    _edinet(*retained)
    _, cache = retained
    (cache / "edinet/S100NEW.zip").write_bytes(b"not a zip")
    importer, objects, _, companies = _importer(retained, lambda _: pytest.fail("malformed source cannot derive"))
    with pytest.raises(RetainedInputError) as error:
        importer.import_ticker("6752.T", preserve_unavailable=True)
    assert isinstance(error.value.__cause__, edinet.EdinetError)
    assert b"not a zip" not in objects.data.values()
    assert not companies.calls


def test_sec_acquisition_is_unchanged_by_edinet_ceiling(retained, monkeypatch):
    _sec(*retained)
    monkeypatch.setattr(edinet, "_XBRL_INPUT_LIMIT", 1)
    importer, _, _, companies = _importer(retained, lambda bundle: ("ok", {"ticker": bundle.ticker}))
    assert importer.import_ticker("ABT").data_status == "AVAILABLE"
    assert companies.calls[-1]["issuer_source"] == "SEC"
