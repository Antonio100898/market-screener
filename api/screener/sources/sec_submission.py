"""Decode exact named documents from bounded retained SEC submission bytes."""
from dataclasses import dataclass
import hashlib

from screener.sources.inline_xbrl import safe_document_name


EXTRACTION_REVISION = "SEC_NAMED_DOCUMENTS_1"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_SECTIONS = 256
MAX_REQUESTS = 64
MAX_HEADER_BYTES = 64 * 1024
MAX_NAME_BYTES = 4096
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_WORK_BYTES = 48 * 1024 * 1024


@dataclass(frozen=True)
class DocumentPart:
    filename: str
    document_type: str
    sequence: int
    description: str | None
    text_separator: str
    enclosure: str
    raw_text_start: int
    raw_text_end: int
    raw_text_sha256: str
    content_start: int
    content_end: int
    content_sha256: str
    payload: bytes


@dataclass(frozen=True)
class SubmissionDocuments:
    container_sha256: str
    container_byte_size: int
    extraction_revision: str
    fidelity: str
    section_count: int
    work_bytes: int
    documents: tuple[DocumentPart, ...]


def _name(value):
    if (type(value) is not str or len(value) > MAX_NAME_BYTES
            or len(value.encode("utf-8")) > MAX_NAME_BYTES):
        raise ValueError("submission document name exceeds its bound")
    name = safe_document_name(value)
    if name in {".", ".."}:
        raise ValueError("submission document name is not a file")
    return name


def decode_named_documents(body: bytes, filenames: tuple[str, ...]) -> SubmissionDocuments:
    if type(body) is not bytes or not 0 < len(body) <= MAX_INPUT_BYTES:
        raise ValueError("submission container requires bounded exact bytes")
    if type(filenames) is not tuple or not 0 < len(filenames) <= MAX_REQUESTS:
        raise ValueError("submission requires a bounded tuple of names")
    names = tuple(_name(name) for name in filenames)
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("submission request names are duplicate or ambiguous")
    requested = set(names)
    records, seen = {}, set()
    enclosure_tag, enclosure_closed = None, False
    content_start, content_end = 0, None
    state, opened_header, sections, fields = "preamble", False, 0, {}
    position, header_start, raw_start, separator = 0, 0, 0, b""
    # Input hashing and delimiter scanning each consume at most the input size.
    work = 2 * len(body)
    while position < len(body):
        newline = body.find(b"\n", position)
        if newline < 0:
            token_end, next_position, ending = len(body), len(body), b""
        else:
            ending = b"\r\n" if newline > position and body[newline - 1] == 13 else b"\n"
            token_end = newline - (len(ending) - 1)
            next_position = newline + 1
        work += token_end - position
        if work > MAX_WORK_BYTES:
            raise ValueError("submission scanner work exceeds its bound")
        token = body[position:token_end]
        if state != "preamble" and token.startswith((b"<SEC-HEADER>", b"</SEC-HEADER>")):
            raise ValueError("submission header record is misplaced")
        if position > 0 and token.startswith(b"<SEC-DOCUMENT>"):
            raise ValueError("submission opening record is repeated")
        if position == 0 and not token.startswith(b"<SEC-DOCUMENT>"):
            raise ValueError("submission container lacks its opening record")

        if state == "preamble":
            if token.startswith(b"<SEC-HEADER>"):
                if opened_header:
                    raise ValueError("submission header is repeated")
                opened_header = True
            elif token == b"</SEC-HEADER>":
                if not opened_header:
                    raise ValueError("submission header is not open")
                state = "outside"
            elif token in {b"<DOCUMENT>", b"</DOCUMENT>", b"<TEXT>", b"</TEXT>", b"</SEC-DOCUMENT>"}:
                raise ValueError("submission header framing is incomplete")
            if next_position > MAX_HEADER_BYTES:
                raise ValueError("submission header exceeds its bound")
        elif state == "outside":
            if token == b"<DOCUMENT>" and ending:
                sections += 1
                if sections > MAX_SECTIONS:
                    raise ValueError("submission section count exceeds its bound")
                header_start, fields, state = next_position, {}, "document_header"
            elif token == b"</SEC-DOCUMENT>":
                state = "closed"
            elif token:
                raise ValueError("submission has content outside a document")
        elif state == "document_header":
            if next_position - header_start > MAX_HEADER_BYTES:
                raise ValueError("submission document header exceeds its bound")
            if token == b"<TEXT>" and ending:
                if not {"TYPE", "SEQUENCE", "FILENAME"}.issubset(fields):
                    raise ValueError("submission document header is incomplete")
                filename = _name(fields["FILENAME"])
                if filename.casefold() in seen:
                    raise ValueError("submission document names are duplicate or ambiguous")
                seen.add(filename.casefold())
                sequence = fields["SEQUENCE"]
                if (not sequence.isascii() or not sequence.isdecimal() or len(sequence) > 19
                        or not 0 < int(sequence) <= 2**63 - 1):
                    raise ValueError("submission document sequence is invalid")
                if not fields["TYPE"]:
                    raise ValueError("submission document type is empty")
                raw_start, separator, state = token_end, ending, "text"
                enclosure_tag, enclosure_closed = None, False
                content_start, content_end = raw_start, None
            else:
                tag, marker, value = token.partition(b">")
                if not marker or tag not in {b"<TYPE", b"<SEQUENCE", b"<FILENAME", b"<DESCRIPTION"}:
                    raise ValueError("submission document header field is unsupported")
                key = tag[1:].decode("ascii")
                if key in fields or len(value) > MAX_NAME_BYTES:
                    raise ValueError("submission document header field is duplicate or oversized")
                fields[key] = value.decode("utf-8", errors="strict")
        elif state == "text":
            if (token.startswith((b"<XML>", b"<XBRL>", b"</XML>", b"</XBRL>"))
                    and token not in {b"<XML>", b"<XBRL>", b"</XML>", b"</XBRL>"}):
                raise ValueError("submission enclosure delimiter is malformed")
            if token == b"</TEXT>" and ending:
                if enclosure_tag is not None and not enclosure_closed:
                    raise ValueError("submission document enclosure is truncated")
                filename = fields["FILENAME"]
                if filename in requested:
                    final_content_end = position if content_end is None else content_end
                    enclosure = "NONE" if enclosure_tag is None else enclosure_tag.decode("ascii")
                    if final_content_end <= content_start:
                        raise ValueError("submission selected content is empty")
                    records[filename] = dict(
                        filename=filename, document_type=fields["TYPE"], sequence=int(fields["SEQUENCE"]),
                        description=fields.get("DESCRIPTION"), text_separator="LF" if separator == b"\n" else "CRLF",
                        enclosure=enclosure, raw_text_start=raw_start, raw_text_end=position,
                        content_start=content_start, content_end=final_content_end,
                    )
                state = "document_close"
            elif enclosure_closed:
                raise ValueError("submission has content after its enclosure")
            elif token in {b"<XML>", b"<XBRL>"}:
                if enclosure_tag is not None or position != raw_start + len(separator) or ending != separator:
                    raise ValueError("submission has nested or misplaced enclosure")
                enclosure_tag = token[1:-1]
                content_start = next_position
            elif token in {b"</XML>", b"</XBRL>"}:
                if enclosure_tag != token[2:-1] or ending != separator:
                    raise ValueError("submission enclosure close differs")
                enclosure_closed = True
                content_end = position
            elif token in {b"<DOCUMENT>", b"</DOCUMENT>", b"<TEXT>", b"</SEC-DOCUMENT>"}:
                raise ValueError("submission text has nested or truncated document framing")
        elif state == "document_close":
            if token != b"</DOCUMENT>" or not ending:
                raise ValueError("submission document close is missing")
            state = "outside"
        elif token:
            raise ValueError("submission has trailing content after its close")
        position = next_position

    if state != "closed" or sections == 0:
        raise ValueError("submission container is truncated")
    if set(records) != requested:
        raise ValueError("submission requested document is missing")
    metadata = [records[name] for name in names]
    output_size = sum(part["content_end"] - part["content_start"] for part in metadata)
    if output_size > MAX_OUTPUT_BYTES:
        raise ValueError("submission output exceeds its aggregate bound")
    for part in metadata:
        raw_size = part["raw_text_end"] - part["raw_text_start"]
        content_size = part["content_end"] - part["content_start"]
        if raw_size > MAX_PART_BYTES or not 0 < content_size <= MAX_PART_BYTES:
            raise ValueError("submission selected part exceeds its bound")
        work += raw_size + 2 * content_size
    if work > MAX_WORK_BYTES:
        raise ValueError("submission extraction work exceeds its bound")
    container_sha256 = hashlib.sha256(body).hexdigest()
    documents, view = [], memoryview(body)
    for part in metadata:
        raw = view[part["raw_text_start"]:part["raw_text_end"]]
        content = view[part["content_start"]:part["content_end"]]
        documents.append(DocumentPart(
            **part, raw_text_sha256=hashlib.sha256(raw).hexdigest(),
            content_sha256=hashlib.sha256(content).hexdigest(), payload=bytes(content),
        ))
    return SubmissionDocuments(
        container_sha256, len(body), EXTRACTION_REVISION, "EXACT_CONTAINER_BYTES",
        sections, work, tuple(documents),
    )
