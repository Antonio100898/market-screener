"""Exact framing and capacity controls for SEC submission documents."""
import hashlib
import tracemalloc

import pytest

from screener.sources import sec_submission


@pytest.fixture(scope="module")
def parser():
    return sec_submission

def _document(name="a.txt", payload=b"data\n", enclosure=None, sequence=7, separator=b"\n"):
    prefix = separator.join((
        b"<DOCUMENT>", b"<TYPE>XML", f"<SEQUENCE>{sequence}".encode(),
        b"<FILENAME>" + name.encode(), b"<DESCRIPTION>labelled synthetic part", b"<TEXT>",
    ))
    if enclosure is not None:
        payload = b"<" + enclosure + b">" + separator + payload + b"</" + enclosure + b">" + separator
    return prefix + separator + payload + b"</TEXT>" + separator + b"</DOCUMENT>" + separator


def _submission(*documents, separator=b"\n"):
    return (separator.join((b"<SEC-DOCUMENT>fixture", b"<SEC-HEADER>fixture", b"</SEC-HEADER>"))
            + separator + b"".join(documents) + b"</SEC-DOCUMENT>" + separator)


@pytest.mark.parametrize("separator", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("enclosure", [None, b"XML", b"XBRL"], ids=["plain", "XML", "XBRL"])
def test_plain_and_enclosed_exact_bytes(parser, separator, enclosure):
    payload = b"<node> \xff utf8:\xe2\x82\xac  </node>" + separator
    body = _submission(_document(payload=payload, enclosure=enclosure, separator=separator), separator=separator)
    result = parser.decode_named_documents(body, ("a.txt",))
    part = result.documents[0]
    expected_content = separator + payload if enclosure is None else payload
    expected_raw = separator + payload if enclosure is None else (
        separator + b"<" + enclosure + b">" + separator + payload + b"</" + enclosure + b">" + separator)
    assert part.payload == expected_content
    assert body[part.raw_text_start:part.raw_text_end] == expected_raw
    assert body[part.content_start:part.content_end] == expected_content
    assert part.raw_text_sha256 == hashlib.sha256(expected_raw).hexdigest()
    assert part.content_sha256 == hashlib.sha256(expected_content).hexdigest()
    assert part.text_separator == ("LF" if separator == b"\n" else "CRLF")
    assert part.enclosure == ("NONE" if enclosure is None else enclosure.decode())


@pytest.mark.parametrize("case", [
    "header_missing", "header_repeated", "opening_repeated", "text_truncated", "document_truncated", "container_truncated",
    "document_nested", "text_nested", "document_close_stray", "enclosure_nested", "enclosure_wrong_close",
    "enclosure_truncated", "enclosure_malformed", "after_enclosure", "empty_enclosure", "duplicate_name", "casefold_name",
    "unsafe_name", "dotdot_name", "unknown_header", "duplicate_header_field", "missing_type", "empty_type", "bad_sequence",
    "invalid_header_utf8",
    "sec_open_requested", "sec_close_requested", "sec_open_unrequested", "sec_close_unrequested",
    "unrequested_nested_enclosure", "unrequested_wrong_enclosure", "unrequested_malformed_enclosure", "mixed_enclosure_separator",
])
def test_malformed_framing_headers_and_names_refuse(parser, case):
    body = _submission(_document(enclosure=b"XML"))
    mutations = {
        "header_missing": (b"</SEC-HEADER>\n", b""),
        "header_repeated": (b"<SEC-HEADER>fixture\n", b"<SEC-HEADER>fixture\n<SEC-HEADER>again\n"),
        "opening_repeated": (b"</SEC-HEADER>\n", b"<SEC-DOCUMENT>again\n</SEC-HEADER>\n"),
        "text_truncated": (b"</TEXT>\n", b""),
        "document_truncated": (b"</DOCUMENT>\n", b""),
        "container_truncated": (b"</SEC-DOCUMENT>\n", b""),
        "document_nested": (b"data\n", b"<DOCUMENT>\n"),
        "text_nested": (b"data\n", b"<TEXT>\n"),
        "document_close_stray": (b"data\n", b"</DOCUMENT>\n"),
        "enclosure_nested": (b"data\n", b"<XBRL>\ndata\n</XBRL>\n"),
        "enclosure_wrong_close": (b"</XML>\n", b"</XBRL>\n"),
        "enclosure_truncated": (b"</XML>\n", b""),
        "enclosure_malformed": (b"<XML>\n", b"<XML>wrong\n"),
        "after_enclosure": (b"</XML>\n", b"</XML>\nextra\n"),
        "empty_enclosure": (b"data\n", b""),
        "unsafe_name": (b"<FILENAME>a.txt", b"<FILENAME>../a.txt"),
        "dotdot_name": (b"<FILENAME>a.txt", b"<FILENAME>.."),
        "unknown_header": (b"<TYPE>XML\n", b"<EXTRA>value\n<TYPE>XML\n"),
        "duplicate_header_field": (b"<TYPE>XML\n", b"<TYPE>XML\n<TYPE>XML\n"),
        "missing_type": (b"<TYPE>XML\n", b""),
        "empty_type": (b"<TYPE>XML\n", b"<TYPE>\n"),
        "bad_sequence": (b"<SEQUENCE>7", b"<SEQUENCE>-7"),
        "invalid_header_utf8": (b"<TYPE>XML", b"<TYPE>\xff"),
    }
    if case.startswith("sec_"):
        token = b"<SEC-HEADER>stray\n" if "open" in case else b"</SEC-HEADER>\n"
        damaged = _document(enclosure=b"XML").replace(b"data\n", token, 1)
        if case.endswith("unrequested"):
            damaged = damaged.replace(b"<FILENAME>a.txt", b"<FILENAME>ignored.txt", 1)
            body = _submission(_document(), damaged)
        else:
            body = _submission(damaged)
    elif case.startswith("unrequested_"):
        damaged = _document(name="ignored.txt", enclosure=b"XML")
        changes = {
            "unrequested_nested_enclosure": (b"data\n", b"<XBRL>\ndata\n</XBRL>\n"),
            "unrequested_wrong_enclosure": (b"</XML>\n", b"</XBRL>\n"),
            "unrequested_malformed_enclosure": (b"<XML>\n", b"<XML>wrong\n"),
        }
        before, after = changes[case]
        body = _submission(_document(), damaged.replace(before, after, 1))
    elif case == "mixed_enclosure_separator":
        body = body.replace(b"<XML>\n", b"<XML>\r\n", 1)
    elif case == "duplicate_name":
        body = _submission(_document(), _document(sequence=9))
    elif case == "casefold_name":
        body = _submission(_document(), _document(name="A.txt", sequence=9))
    else:
        before, after = mutations[case]
        assert body.count(before) == 1
        body = body.replace(before, after, 1)
    with pytest.raises(ValueError):
        parser.decode_named_documents(body, ("a.txt",))


@pytest.mark.parametrize("case", ["empty_bytes", "bytearray", "empty_requests", "list_requests", "duplicate_requests", "casefold_requests", "unsafe_request", "missing_request"])
def test_input_and_request_contract_refusals(parser, case):
    body, names = _submission(_document()), ("a.txt",)
    if case == "empty_bytes": body = b""
    elif case == "bytearray": body = bytearray(body)
    elif case == "empty_requests": names = ()
    elif case == "list_requests": names = ["a.txt"]
    elif case == "duplicate_requests": names = ("a.txt", "a.txt")
    elif case == "casefold_requests": names = ("a.txt", "A.txt")
    elif case == "unsafe_request": names = ("../a.txt",)
    elif case == "missing_request": names = ("other.txt",)
    with pytest.raises(ValueError):
        parser.decode_named_documents(body, names)


def test_declared_byte_name_request_section_limits(parser):
    empty = _submission(_document(payload=b""))
    payload_size = parser.MAX_INPUT_BYTES - len(empty)
    full = _submission(_document(payload=b"x" * (payload_size - 1) + b"\n"))
    assert len(full) == parser.MAX_INPUT_BYTES
    assert parser.decode_named_documents(full, ("a.txt",)).container_byte_size == parser.MAX_INPUT_BYTES
    with pytest.raises(ValueError, match="bounded exact bytes"):
        parser.decode_named_documents(full + b"\n", ("a.txt",))
    names = tuple(f"doc{i}.txt" for i in range(parser.MAX_REQUESTS))
    body = _submission(*(_document(name=name, sequence=i + 1) for i, name in enumerate(names)))
    assert len(parser.decode_named_documents(body, tuple(reversed(names))).documents) == parser.MAX_REQUESTS
    with pytest.raises(ValueError, match="bounded tuple"):
        parser.decode_named_documents(body, names + ("extra.txt",))
    sections = tuple(_document(name=f"section{i}.txt", sequence=i + 1) for i in range(parser.MAX_SECTIONS))
    assert parser.decode_named_documents(_submission(*sections), ("section0.txt",)).section_count == parser.MAX_SECTIONS
    with pytest.raises(ValueError, match="section count"):
        parser.decode_named_documents(_submission(*sections, _document(name="extra.txt", sequence=999)), ("section0.txt",))
    name = "n" * (parser.MAX_NAME_BYTES - 4) + ".txt"
    assert parser.decode_named_documents(_submission(_document(name=name)), (name,)).documents[0].filename == name
    with pytest.raises(ValueError, match="name exceeds"):
        parser.decode_named_documents(body, (name + "n",))
    exact = _submission(_document(name="MixedCase.txt", sequence=111), _document(name="Other.txt", sequence=3))
    ordered = parser.decode_named_documents(exact, ("Other.txt", "MixedCase.txt"))
    assert tuple(part.filename for part in ordered.documents) == ("Other.txt", "MixedCase.txt")
    assert tuple(part.sequence for part in ordered.documents) == (3, 111)
    with pytest.raises(ValueError, match="requested document is missing"):
        parser.decode_named_documents(exact, ("mixedcase.txt",))
    oversized = _submission(_document()).replace(b"<TYPE>XML", b"<TYPE>" + b"x" * (parser.MAX_NAME_BYTES + 1))
    with pytest.raises(ValueError, match="oversized"):
        parser.decode_named_documents(oversized, ("a.txt",))


def test_header_part_output_and_work_frontiers(parser, monkeypatch):
    body = _submission(_document(enclosure=b"XML"))
    result = parser.decode_named_documents(body, ("a.txt",))
    part = result.documents[0]
    header_end = body.index(b"</SEC-HEADER>\n") + len(b"</SEC-HEADER>\n")
    with monkeypatch.context() as patch:
        patch.setattr(parser, "MAX_HEADER_BYTES", header_end - 1)
        with pytest.raises(ValueError, match="header exceeds"):
            parser.decode_named_documents(body, ("a.txt",))
    long_header = _submission(_document()).replace(b"<DESCRIPTION>labelled synthetic part", b"<DESCRIPTION>" + b"h" * 1000)
    document_start = long_header.index(b"<DOCUMENT>\n") + len(b"<DOCUMENT>\n")
    document_end = long_header.index(b"<TEXT>\n") + len(b"<TEXT>\n")
    with monkeypatch.context() as patch:
        patch.setattr(parser, "MAX_HEADER_BYTES", document_end - document_start - 1)
        with pytest.raises(ValueError, match="document header exceeds"):
            parser.decode_named_documents(long_header, ("a.txt",))
    allocation_body = _submission(_document()).replace(b"<SEC-DOCUMENT>fixture", b"<SEC-DOCUMENT>" + b"x" * (1024 * 1024), 1)
    with monkeypatch.context() as patch:
        patch.setattr(parser, "MAX_WORK_BYTES", 2 * len(allocation_body))
        tracemalloc.start()
        try:
            try:
                parser.decode_named_documents(allocation_body, ("a.txt",))
            except ValueError as error:
                assert "scanner work" in str(error)
            else:
                raise AssertionError("copy work limit was not enforced")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 * 1024, "refused first token copied the one-MiB line"
    for attribute, limit, reason in (
        ("MAX_PART_BYTES", part.raw_text_end - part.raw_text_start - 1, "selected part"),
        ("MAX_OUTPUT_BYTES", len(part.payload) - 1, "output exceeds"),
        ("MAX_WORK_BYTES", result.work_bytes - 1, "extraction work"),
        ("MAX_WORK_BYTES", 2 * len(body) - 1, "scanner work"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(parser, attribute, limit)
            def no_hash(*args, **kwargs):
                raise AssertionError("refused extraction reached output hashing")
            patch.setattr(parser.hashlib, "sha256", no_hash)
            with pytest.raises(ValueError, match=reason):
                parser.decode_named_documents(body, ("a.txt",))
    with monkeypatch.context() as patch:
        patch.setattr(parser, "MAX_WORK_BYTES", result.work_bytes)
        patch.setattr(parser, "MAX_OUTPUT_BYTES", len(part.payload))
        assert parser.decode_named_documents(body, ("a.txt",)).work_bytes == result.work_bytes
