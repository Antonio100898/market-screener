#!/usr/bin/env python3
"""Build and inspect the frozen SEC extraction pilot without network access."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import re
import shutil
import socket
import tempfile
from typing import Any


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROPOSAL = REPO / "local/llm-statement-extraction-proposal-2026-09-28"
RAW_DEFAULT = HERE / "raw"
ARTIFACTS_DEFAULT = HERE / "artifacts"
CANONICAL_SCHEMA_PATH = PROPOSAL / "candidate.schema.json"
PROMPT_PATH = PROPOSAL / "prompt.md"

BASELINE_SYSTEM = (
    "Extract the requested financial statement fields from the supplied official filing documents.\n"
    "Return JSON matching the supplied schema."
)

RAW_FILES = {
    "000119312526197494-index.json": {
        "sha256": "eedf4cfda3a6a7f923464b3321abd4eb3be4e4154cd1bcd53da113f439449b4c",
        "accession": "0001193125-26-197494",
        "document": "index.json",
        "media_type": "application/json",
    },
    "d101275d20f.htm": {
        "sha256": "b842756f7ce62b0ae1bd2cf14d6fc3373c61297c0a74934f020493332415326e",
        "accession": "0001193125-26-197494",
        "document": "d101275d20f.htm",
        "media_type": "text/html",
    },
    "000205889726000125-index.json": {
        "sha256": "ae1d8e729b51b2a3479b2c740f0882ad6fcb4961ffd9299b45ff7654a2955c4d",
        "accession": "0002058897-26-000125",
        "document": "index.json",
        "media_type": "application/json",
    },
    "cib-20251231.htm": {
        "sha256": "f05d92a8c53a451afc40d649fed70f0384d35cc8792f00f2752c9fcd43d7a271",
        "accession": "0002058897-26-000125",
        "document": "cib-20251231.htm",
        "media_type": "text/html",
    },
    "000110465926010352-index.json": {
        "sha256": "50e83ef941db63b3d5020c0d851837cd873dcaedebe220cb4fb22db8d4624c89",
        "accession": "0001104659-26-010352",
        "document": "index.json",
        "media_type": "application/json",
    },
    "tm261145d1_40f.htm": {
        "sha256": "7490bb0e34394e55e1e349e7f3c44581fe3bfe50ee02b368a1c4b938af13ab2f",
        "accession": "0001104659-26-010352",
        "document": "tm261145d1_40f.htm",
        "media_type": "text/html",
    },
    "000001686826000011-index.json": {
        "sha256": "78fc8952f13a716fd9606b5ac2812020fcb6d84da3f58605b518a268868f2778",
        "accession": "0000016868-26-000011",
        "document": "index.json",
        "media_type": "application/json",
    },
    "cni-20251231.htm": {
        "sha256": "a3789c3b3a1a2a1b2ee15a6985c6e757c699c8c2992436ec6036b4e985fbb79d",
        "accession": "0000016868-26-000011",
        "document": "cni-20251231.htm",
        "media_type": "text/html",
    },
}

FIELD_DEFINITIONS = {
    "AssetsCurrent": {
        "definition": "Total current assets explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "Assets": {
        "definition": "Total assets explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "LiabilitiesCurrent": {
        "definition": "Total current liabilities explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "Liabilities": {
        "definition": "Total liabilities explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "Equity": {
        "definition": "Total equity explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "EquityAndLiabilities": {
        "definition": "Total equity and liabilities explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "LiabilitiesAndStockholdersEquity": {
        "definition": "Total liabilities and stockholders' equity explicitly reported on the consolidated primary balance sheet.",
        "period_kind": "instant",
    },
    "NetCashProvidedByUsedInOperatingActivities": {
        "definition": "Net cash provided by or used in operating activities explicitly reported on the consolidated primary cash-flow statement.",
        "period_kind": "duration",
    },
    "NetIncomeLoss": {
        "definition": "Consolidated net income or loss explicitly reported on the primary income statement.",
        "period_kind": "duration",
    },
    "NetIncomeLossAttributableToOwnersOfParent": {
        "definition": "Net income or loss attributable to owners of the parent explicitly reported on the consolidated primary income statement.",
        "period_kind": "duration",
    },
}


def requested_fields(names: list[str], end: str, start: str | None = None) -> list[dict[str, Any]]:
    result = []
    for name in names:
        field = FIELD_DEFINITIONS[name]
        result.append(
            {
                "canonical_name": name,
                "definition": field["definition"],
                "period_kind": field["period_kind"],
                "target_start": start if field["period_kind"] == "duration" else None,
                "target_end": end,
                "required_scope": "consolidated",
            }
        )
    return result


CASES = {
    "aero-primary": {
        "request_id": "pilot-aero-primary-v1",
        "issuer": {"source": "SEC", "cik": "0001561861", "ticker": "AERO"},
        "documents": ["d101275d20f.htm"],
        "fields": requested_fields(
            [
                "AssetsCurrent",
                "Assets",
                "LiabilitiesCurrent",
                "Liabilities",
                "Equity",
                "EquityAndLiabilities",
                "NetCashProvidedByUsedInOperatingActivities",
            ],
            "2025-12-31",
            "2025-01-01",
        ),
        "relationship": "Form 20-F primary document",
    },
    "cib-primary": {
        "request_id": "pilot-cib-primary-v1",
        "issuer": {"source": "SEC", "cik": "0002058897", "ticker": "CIB"},
        "documents": ["cib-20251231.htm"],
        "fields": requested_fields(
            [
                "Assets",
                "Liabilities",
                "Equity",
                "EquityAndLiabilities",
                "NetIncomeLossAttributableToOwnersOfParent",
            ],
            "2025-12-31",
            "2025-01-01",
        ),
        "relationship": "Form 20-F primary document",
    },
    "cni-exhibit": {
        "request_id": "pilot-cni-exhibit-v1",
        "issuer": {"source": "SEC", "cik": "0000016868", "ticker": "CNI"},
        "documents": ["tm261145d1_40f.htm", "cni-20251231.htm"],
        "fields": requested_fields(
            [
                "AssetsCurrent",
                "Assets",
                "LiabilitiesCurrent",
                "Liabilities",
                "LiabilitiesAndStockholdersEquity",
                "NetIncomeLoss",
                "NetCashProvidedByUsedInOperatingActivities",
            ],
            "2025-12-31",
            "2025-01-01",
        ),
        "relationship": "Form 40-F wrapper and its incorporated Exhibit 99.2",
    },
    "aero-summary-neighbour": {
        "request_id": "pilot-aero-summary-neighbour-v1",
        "issuer": {"source": "SEC", "cik": "0001561861", "ticker": "AERO"},
        "documents": ["d101275d20f.htm"],
        "fields": requested_fields(["Assets"], "2025-12-31"),
        "relationship": "Form 20-F primary document containing both summaries and audited statements",
    },
    "cib-unclassified-neighbour": {
        "request_id": "pilot-cib-unclassified-neighbour-v1",
        "issuer": {"source": "SEC", "cik": "0002058897", "ticker": "CIB"},
        "documents": ["cib-20251231.htm"],
        "fields": requested_fields(["AssetsCurrent", "LiabilitiesCurrent"], "2025-12-31"),
        "relationship": "Form 20-F primary document with an unclassified bank balance sheet",
    },
    "cni-wrapper-neighbour": {
        "request_id": "pilot-cni-wrapper-neighbour-v1",
        "issuer": {"source": "SEC", "cik": "0000016868", "ticker": "CNI"},
        "documents": ["tm261145d1_40f.htm"],
        "fields": requested_fields(
            [
                "AssetsCurrent",
                "Assets",
                "LiabilitiesCurrent",
                "Liabilities",
                "LiabilitiesAndStockholdersEquity",
                "NetIncomeLoss",
                "NetCashProvidedByUsedInOperatingActivities",
            ],
            "2025-12-31",
            "2025-01-01",
        ),
        "relationship": "Form 40-F wrapper only; incorporated Exhibit 99.2 is intentionally absent",
    },
}

PROVIDERS = {
    "openai": {
        "model": "gpt-6-luna",
        "endpoint": "https://api.openai.com/v1/responses",
        "context_limit": 1_050_000,
        "max_output_tokens": 8_192,
        "input_price": 0.10,
        "output_price": 0.50,
        "status": "CONFIG_READY",
    },
    "fireworks": {
        "model": "accounts/fireworks/models/glm-5p3-flash",
        "endpoint": "https://api.fireworks.ai/inference/v1/chat/completions",
        "context_limit": 1_040_000,
        "max_output_tokens": 8_192,
        "input_price": 0.15,
        "output_price": 0.50,
        "status": "CONFIG_READY",
    },
    "google": {
        "model": "gemini-3.5-flash-lite",
        "endpoint": "https://generativelanguage.googleapis.com/v1/interactions",
        "context_limit": 1_048_576,
        "max_output_tokens": 8_192,
        "input_price": 0.30,
        "output_price": 2.50,
        "status": "CONFIG_READY",
    },
    "anthropic": {
        "model": "claude-sonnet-5",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "context_limit": 1_000_000,
        "max_output_tokens": 8_192,
        "input_price": 2.00,
        "output_price": 10.00,
        "status": "CONFIG_READY",
    },
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def verify_raw(raw_dir: Path) -> dict[str, dict[str, Any]]:
    verified = {}
    for filename, expected in RAW_FILES.items():
        path = raw_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"missing frozen source: {path}")
        payload = path.read_bytes()
        observed = sha256_bytes(payload)
        if observed != expected["sha256"]:
            raise ValueError(f"frozen hash mismatch for {filename}: {observed}")
        verified[filename] = {**expected, "byte_size": len(payload), "path": path}
    return verified


def extract_candidate_system() -> str:
    source = PROMPT_PATH.read_text()
    start_marker = "## System message\n"
    end_marker = "\n## Request message template"
    start = source.index(start_marker) + len(start_marker)
    end = source.index(end_marker, start)
    return source[start:end].strip()


class SourceViewParser(HTMLParser):
    """Create an ordered visible-text view and a raw-span map."""

    HIDDEN_TAGS = {"script", "style", "noscript", "ix:hidden", "ix:header"}
    BLOCK_TAGS = {"address", "article", "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "title"}
    ALWAYS_KEEP_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "title"}
    FINANCIAL_BLOCK = re.compile(
        r"\b(?:assets?|liabilit(?:y|ies)|equity|cash flows?|income|balance sheets?|"
        r"financial statements?|consolidated|statement of|year ended|as of|ifrs|"
        r"u\.s\. dollars?|millions?|thousands?|management|summary|exhibit|"
        r"incorporated by reference)\b",
        re.I,
    )

    def __init__(self, raw_text: str, document_id: str, raw_sha256: str, raw_filename: str):
        super().__init__(convert_charrefs=False)
        self.raw_text = raw_text
        self.document_id = document_id
        self.raw_sha256 = raw_sha256
        self.raw_filename = raw_filename
        self.line_starts = [0]
        self.line_starts.extend(match.end() for match in re.finditer("\n", raw_text))
        self.output: list[str] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.tag_stack: list[tuple[str, bool]] = []
        self.hidden_depth = 0
        self.table_stack: list[dict[str, int]] = []
        self.open_cells: list[dict[str, Any]] = []
        self.open_blocks: list[dict[str, Any]] = []
        self.table_count = 0
        self.block_count = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def _write(self, text: str) -> None:
        self.output.append(text)

    def _newline(self) -> None:
        if self.output and not self.output[-1].endswith("\n"):
            self.output.append("\n")

    def _new_record(self, source_id: str, kind: str, start: int, **extra: Any) -> None:
        self.records[source_id] = {
            "kind": kind,
            "raw_filename": self.raw_filename,
            "raw_sha256": self.raw_sha256,
            "raw_char_start": start,
            "raw_char_end": None,
            **extra,
        }

    def _close_record(self, source_id: str, end: int) -> None:
        self.records[source_id]["raw_char_end"] = max(end, self.records[source_id]["raw_char_start"])

    def _ensure_cell_marker(self) -> None:
        if not self.open_cells or self.open_cells[-1]["started"]:
            return
        cell = self.open_cells[-1]
        self._new_record(
            cell["source_id"],
            "table_cell",
            cell["start"],
            table=cell["table"],
            row=cell["row"],
            column=cell["column"],
        )
        cell["started"] = True
        self._write(f"<S:{cell['source_id']}>")

    def _hidden_start(self, tag: str, attrs: dict[str, str]) -> bool:
        style = re.sub(r"\s+", "", attrs.get("style", "")).casefold()
        return tag in self.HIDDEN_TAGS or "display:none" in style or "visibility:hidden" in style

    def handle_starttag(self, tag: str, attr_pairs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs = {key.casefold(): value or "" for key, value in attr_pairs}
        own_hidden = self._hidden_start(tag, attrs)
        self.tag_stack.append((tag, own_hidden))
        if own_hidden:
            self.hidden_depth += 1
        if self.hidden_depth:
            return

        start = self._offset()
        if tag == "table":
            self._ensure_cell_marker()
            self.table_count += 1
            self.table_stack.append({"table": self.table_count, "row": 0, "column": 0})
            self._newline()
            self._write(f"<T:{self.document_id}:{self.table_count}>\n")
        elif tag == "tr" and self.table_stack:
            state = self.table_stack[-1]
            state["row"] += 1
            state["column"] = 0
            self._newline()
        elif tag in {"td", "th"} and self.table_stack:
            state = self.table_stack[-1]
            state["column"] += 1
            source_id = f"{self.document_id}:C{state['table']}.{state['row']}.{state['column']}"
            self.open_cells.append(
                {
                    "source_id": source_id,
                    "depth": len(self.table_stack),
                    "start": start,
                    "table": state["table"],
                    "row": state["row"],
                    "column": state["column"],
                    "started": False,
                }
            )
        elif tag in self.BLOCK_TAGS and not self.table_stack:
            self.block_count += 1
            source_id = f"{self.document_id}:B{self.block_count}"
            self.open_blocks.append(
                {"source_id": source_id, "depth": len(self.tag_stack), "start": start, "tag": tag, "parts": []}
            )
        elif tag == "br":
            self._newline()

        anchor = attrs.get("id")
        if anchor:
            source_id = f"{self.document_id}:A:{anchor}"
            self._new_record(source_id, "html_anchor", start, anchor=anchor, tag=tag)
            self._close_record(source_id, start + len(self.get_starttag_text() or ""))
            fact_bits = []
            aliases = {"name": "n", "contextref": "ctx", "unitref": "u", "scale": "s", "decimals": "d", "sign": "sgn"}
            for key in ("name", "contextref", "unitref", "scale", "decimals", "sign"):
                if attrs.get(key):
                    fact_bits.append(f"{aliases[key]}={attrs[key]}")
            suffix = ";" + ";".join(fact_bits) if fact_bits else ""
            marker = f"<S:{source_id}{suffix}>"
            if self.open_blocks and not self.table_stack:
                self.open_blocks[-1]["parts"].append(marker)
            else:
                self._ensure_cell_marker()
                self._write(marker)

        if tag == "a" and attrs.get("href"):
            marker = f"<LINK:{attrs['href']}>"
            if self.open_blocks and not self.table_stack:
                self.open_blocks[-1]["parts"].append(marker)
            else:
                self._ensure_cell_marker()
                self._write(marker)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        end_start = self._offset()
        if not self.hidden_depth:
            if tag in {"td", "th"} and self.open_cells:
                cell = self.open_cells[-1]
                if cell["depth"] == len(self.table_stack):
                    self.open_cells.pop()
                    if cell["started"]:
                        self._close_record(cell["source_id"], end_start + len(tag) + 3)
                    self._write(" | ")
            elif tag == "tr" and self.table_stack:
                self._newline()
            elif tag == "table" and self.table_stack:
                state = self.table_stack.pop()
                self._newline()
                self._write(f"</T:{self.document_id}:{state['table']}>\n")
            elif tag in self.BLOCK_TAGS and self.open_blocks:
                for index in range(len(self.open_blocks) - 1, -1, -1):
                    block = self.open_blocks[index]
                    if block["depth"] == len(self.tag_stack):
                        self.open_blocks.pop(index)
                        block_text = re.sub(r"\s+", " ", "".join(block["parts"])).strip()
                        if block_text and (
                            block["tag"] in self.ALWAYS_KEEP_BLOCKS
                            or self.FINANCIAL_BLOCK.search(block_text)
                        ):
                            self._new_record(block["source_id"], "text_block", block["start"], tag=tag)
                            self._close_record(block["source_id"], end_start + len(tag) + 3)
                            self._newline()
                            self._write(f"<S:{block['source_id']}>{block_text}\n")
                        break

        matched_at = None
        for index in range(len(self.tag_stack) - 1, -1, -1):
            if self.tag_stack[index][0] == tag:
                matched_at = index
                break
        if matched_at is not None:
            removed = self.tag_stack[matched_at:]
            self.tag_stack = self.tag_stack[:matched_at]
            self.hidden_depth -= sum(1 for _, own_hidden in removed if own_hidden)

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        value = re.sub(r"\s+", " ", html.unescape(data))
        if not value.strip():
            return
        if self.open_blocks and not self.table_stack:
            self.open_blocks[-1]["parts"].append(value.strip() + " ")
        else:
            self._ensure_cell_marker()
            self._write(value.strip() + " ")

    def handle_entityref(self, name: str) -> None:
        if not self.hidden_depth:
            value = html.unescape(f"&{name};")
            if not value.isspace():
                if self.open_blocks and not self.table_stack:
                    self.open_blocks[-1]["parts"].append(value)
                else:
                    self._ensure_cell_marker()
                    self._write(value)

    def handle_charref(self, name: str) -> None:
        if not self.hidden_depth:
            value = html.unescape(f"&#{name};")
            if not value.isspace():
                if self.open_blocks and not self.table_stack:
                    self.open_blocks[-1]["parts"].append(value)
                else:
                    self._ensure_cell_marker()
                    self._write(value)

    def finish(self) -> tuple[str, dict[str, Any]]:
        for cell in self.open_cells:
            if cell["started"]:
                self._close_record(cell["source_id"], len(self.raw_text))
        body = "".join(self.output)
        body = re.sub(r"[ \t]+\n", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
        emitted_ids = set(re.findall(r"<S:([^;>]+)(?:;[^>]*)?>", body))
        self.records = {source_id: record for source_id, record in self.records.items() if source_id in emitted_ids}
        header = (
            f"@@DOCUMENT id={self.document_id} file={self.raw_filename} "
            f"raw_sha256={self.raw_sha256}\n"
        )
        view = header + body
        mapping = {
            "schema": "source_view_map_v1",
            "document_id": self.document_id,
            "raw_filename": self.raw_filename,
            "raw_sha256": self.raw_sha256,
            "raw_characters": len(self.raw_text),
            "view_sha256": sha256_bytes(view.encode()),
            "source_records": self.records,
        }
        return view, mapping


def build_source_view(path: Path, document_id: str, raw_sha256: str) -> tuple[str, dict[str, Any]]:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="strict")
    parser = SourceViewParser(text, document_id, raw_sha256, path.name)
    parser.feed(text)
    parser.close()
    return parser.finish()


def resolve_refs(value: Any, root: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [resolve_refs(item, root) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$ref"}:
        ref = value["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"external schema reference is forbidden: {ref}")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return resolve_refs(target, root)
    return {key: resolve_refs(item, root) for key, item in value.items()}


def reduce_schema(canonical: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    inlined = resolve_refs(canonical, canonical)
    removed: list[str] = []

    def visit(value: Any, path: str = "$") -> Any:
        if isinstance(value, list):
            return [visit(item, f"{path}[]") for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, item in value.items():
            current = f"{path}.{key}"
            if key in {"$schema", "$id", "$defs", "allOf", "minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems"}:
                removed.append(current)
                continue
            if key == "const":
                result["enum"] = [item]
                continue
            if key == "oneOf":
                result["anyOf"] = visit(item, f"{path}.anyOf")
                continue
            if key == "type" and isinstance(item, list):
                result["anyOf"] = [{"type": member} for member in item]
                continue
            result[key] = visit(item, current)
        properties = result.get("properties")
        if isinstance(properties, dict) and {"html_anchor", "page"}.issubset(properties):
            required = list(result.get("required", []))
            for field in ("html_anchor", "page"):
                if field not in required:
                    required.append(field)
            result["required"] = required
        return result

    return visit(inlined), removed


class SchemaError(ValueError):
    pass


def validate_schema_instance(instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path: str = "$") -> None:
    root = root or schema
    if "$ref" in schema:
        target: Any = root
        for part in schema["$ref"][2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_schema_instance(instance, target, root, path)
        return
    if "allOf" in schema:
        for item in schema["allOf"]:
            validate_schema_instance(instance, item, root, path)
    if "anyOf" in schema:
        passed = 0
        for item in schema["anyOf"]:
            try:
                validate_schema_instance(instance, item, root, path)
                passed += 1
            except SchemaError:
                pass
        if not passed:
            raise SchemaError(f"{path}: no anyOf branch matched")
    if "oneOf" in schema:
        passed = 0
        for item in schema["oneOf"]:
            try:
                validate_schema_instance(instance, item, root, path)
                passed += 1
            except SchemaError:
                pass
        if passed != 1:
            raise SchemaError(f"{path}: expected one oneOf branch, got {passed}")
    if "const" in schema and instance != schema["const"]:
        raise SchemaError(f"{path}: const mismatch")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: enum mismatch")

    types = schema.get("type")
    if types:
        allowed = types if isinstance(types, list) else [types]
        checks = {
            "null": instance is None,
            "object": isinstance(instance, dict),
            "array": isinstance(instance, list),
            "string": isinstance(instance, str),
            "integer": isinstance(instance, int) and not isinstance(instance, bool),
            "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
            "boolean": isinstance(instance, bool),
        }
        if not any(checks.get(name, False) for name in allowed):
            raise SchemaError(f"{path}: expected {allowed}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in instance:
                raise SchemaError(f"{path}: missing {required}")
        if schema.get("additionalProperties") is False:
            extras = set(instance) - set(properties)
            if extras:
                raise SchemaError(f"{path}: extra properties {sorted(extras)}")
        for key, value in instance.items():
            if key in properties:
                validate_schema_instance(value, properties[key], root, f"{path}.{key}")
    elif isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise SchemaError(f"{path}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"{path}: too many items")
        if "items" in schema:
            for index, value in enumerate(instance):
                validate_schema_instance(value, schema["items"], root, f"{path}[{index}]")
    elif isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            raise SchemaError(f"{path}: too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(f"{path}: too long")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise SchemaError(f"{path}: pattern mismatch")
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path}: below minimum")


def example_candidates() -> list[dict[str, Any]]:
    checked = {
        "accession": "0000000000-00-000001",
        "document": "filing.htm",
        "document_sha256": "a" * 64,
        "locator": "Consolidated statement, row Total assets, column 2025",
    }
    return [
        {
            "schema": "statement_candidate_v1",
            "request_id": "schema-test",
            "results": [
                {
                    "requested_field": "Assets",
                    "outcome": "found",
                    "value": "123.45",
                    "scale": "1000000",
                    "source_label": "Total assets",
                    "unit": "USD",
                    "currency": "USD",
                    "period": {"kind": "instant", "start": None, "end": "2025-12-31"},
                    "accounting_basis": "IFRS",
                    "consolidation_scope": "consolidated",
                    "location": {
                        "accession": "0000000000-00-000001",
                        "document": "filing.htm",
                        "document_sha256": "a" * 64,
                        "html_anchor": "fact-1",
                        "page": None,
                        "table": "Consolidated statement of financial position",
                        "row": "Total assets",
                        "column": "2025",
                    },
                    "evidence_quote": "Total assets 123.45",
                }
            ],
        },
        {
            "schema": "statement_candidate_v1",
            "request_id": "schema-test",
            "results": [
                {
                    "requested_field": "AssetsCurrent",
                    "outcome": "not_found",
                    "reason": "The primary balance sheet is unclassified.",
                    "checked_locations": [checked],
                }
            ],
        },
        {
            "schema": "statement_candidate_v1",
            "request_id": "schema-test",
            "results": [
                {
                    "requested_field": "Equity",
                    "outcome": "ambiguous",
                    "reason": "Two scopes remain plausible.",
                    "checked_locations": [checked, {**checked, "locator": "Second plausible row"}],
                }
            ],
        },
    ]


def lint_common_schema(schema: dict[str, Any]) -> None:
    allowed = {"type", "properties", "required", "additionalProperties", "items", "anyOf", "enum"}

    def visit(value: Any, path: str = "$") -> None:
        if not isinstance(value, dict):
            return
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unsupported common schema keys at {path}: {sorted(unknown)}")
        for key, item in value.items():
            if key == "properties":
                for name, subschema in item.items():
                    visit(subschema, f"{path}.properties.{name}")
            elif key == "items":
                visit(item, f"{path}.items")
            elif key == "anyOf":
                for index, subschema in enumerate(item):
                    visit(subschema, f"{path}.anyOf[{index}]")

    visit(schema)


def source_manifest(case: dict[str, Any], verified: dict[str, dict[str, Any]], views: dict[str, dict[str, Any]]) -> dict[str, Any]:
    documents = []
    for filename in case["documents"]:
        item = verified[filename]
        documents.append(
            {
                "accession": item["accession"],
                "document": item["document"],
                "document_sha256": item["sha256"],
                "raw_bytes": item["byte_size"],
                "source_view_sha256": views[filename]["view_sha256"],
                "source_view_bytes": views[filename]["view_bytes"],
            }
        )
    return {"issuer": case["issuer"], "relationship": case["relationship"], "documents": documents}


def request_message(case: dict[str, Any], manifest: dict[str, Any], view_texts: dict[str, str], canonical_schema: dict[str, Any]) -> str:
    documents = "\n".join(view_texts[filename] for filename in case["documents"])
    return (
        f"Request ID: {case['request_id']}\n\n"
        f"Source manifest:\n{json.dumps(manifest, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Requested fields:\n{json.dumps(case['fields'], ensure_ascii=False, sort_keys=True)}\n\n"
        f"Output schema:\n{json.dumps(canonical_schema, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Official filing documents:\n{documents}"
    )


def provider_request(provider: str, system: str, user: str, common_schema: dict[str, Any]) -> dict[str, Any]:
    config = PROVIDERS[provider]
    if provider == "openai":
        return {
            "model": config["model"],
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            "reasoning": {"effort": "none"},
            "max_output_tokens": config["max_output_tokens"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "statement_candidate_v1",
                    "strict": True,
                    "schema": common_schema,
                }
            },
            "store": False,
            "truncation": "disabled",
        }
    if provider == "fireworks":
        return {
            "model": config["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "statement_candidate_v1", "schema": common_schema},
            },
            "max_tokens": config["max_output_tokens"],
            "stream": False,
            "n": 1,
            "context_length_exceeded_behavior": "error",
        }
    if provider == "google":
        return {
            "model": config["model"],
            "system_instruction": system,
            "input": user,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": common_schema,
            },
            "generation_config": {
                "max_output_tokens": config["max_output_tokens"],
                "thinking_level": "minimal",
                "thinking_summaries": "none",
            },
            "store": False,
            "stream": False,
        }
    if provider == "anthropic":
        return {
            "model": config["model"],
            "max_tokens": config["max_output_tokens"],
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "disabled"},
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": common_schema},
            },
        }
    raise KeyError(provider)


def conservative_tokens(byte_size: int) -> int:
    """Use three UTF-8 bytes/token, below the usual four for ASCII financial text."""
    return math.ceil(byte_size / 3)


def request_cost(provider: str, input_tokens: int) -> float:
    config = PROVIDERS[provider]
    input_price = config["input_price"]
    output_price = config["output_price"]
    if provider == "openai" and input_tokens > 272_000:
        input_price *= 2
        output_price *= 1.5
    return input_tokens * input_price / 1_000_000 + config["max_output_tokens"] * output_price / 1_000_000


def build(raw_dir: Path, artifacts_dir: Path) -> dict[str, Any]:
    verified = verify_raw(raw_dir)
    canonical = json.loads(CANONICAL_SCHEMA_PATH.read_text())
    common_schema, removed = reduce_schema(canonical)
    lint_common_schema(common_schema)
    for candidate in example_candidates():
        validate_schema_instance(candidate, canonical)
        validate_schema_instance(candidate, common_schema)

    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    (artifacts_dir / "views").mkdir(parents=True)
    (artifacts_dir / "requests").mkdir()

    view_texts: dict[str, str] = {}
    view_meta: dict[str, dict[str, Any]] = {}
    document_codes = {
        "d101275d20f.htm": "A",
        "cib-20251231.htm": "B",
        "tm261145d1_40f.htm": "W",
        "cni-20251231.htm": "E",
    }
    for filename in document_codes:
        item = verified[filename]
        document_id = document_codes[filename]
        view, mapping = build_source_view(item["path"], document_id, item["sha256"])
        view_path = artifacts_dir / "views" / f"{filename}.view.txt"
        map_path = artifacts_dir / "views" / f"{filename}.map.json"
        view_path.write_text(view)
        map_path.write_bytes(json_bytes(mapping))
        view_texts[filename] = view
        view_meta[filename] = {
            "raw_bytes": item["byte_size"],
            "view_bytes": len(view.encode()),
            "view_sha256": sha256_bytes(view.encode()),
            "map_sha256": sha256_bytes(map_path.read_bytes()),
            "source_records": len(mapping["source_records"]),
        }

    schema_path = artifacts_dir / "provider-common.schema.json"
    schema_path.write_bytes(json_bytes(common_schema))
    transform_path = artifacts_dir / "schema-transform.json"
    transform_path.write_bytes(
        json_bytes(
            {
                "schema": "provider_schema_transform_v1",
                "canonical_sha256": sha256_bytes(CANONICAL_SCHEMA_PATH.read_bytes()),
                "provider_schema_sha256": sha256_bytes(schema_path.read_bytes()),
                "mechanical_rules": [
                    "inline internal references",
                    "replace oneOf with anyOf",
                    "replace const with a one-value enum",
                    "remove allOf and provider-unsupported lexical/numeric/array bounds",
                    "require nullable html_anchor and page properties",
                    "validate every response against the untouched canonical schema",
                ],
                "removed_paths": removed,
            }
        )
    )

    candidate_system = extract_candidate_system()
    request_rows = []
    for case_name, case in CASES.items():
        manifest = source_manifest(case, verified, view_meta)
        user = request_message(case, manifest, view_texts, canonical)
        for provider in PROVIDERS:
            for arm, system in (("baseline", BASELINE_SYSTEM), ("candidate", candidate_system)):
                body = provider_request(provider, system, user, common_schema)
                request_path = artifacts_dir / "requests" / f"{provider}--{case_name}--{arm}.json"
                request_path.write_bytes(json_bytes(body))
                byte_size = request_path.stat().st_size
                input_tokens = conservative_tokens(byte_size)
                config = PROVIDERS[provider]
                fits = input_tokens + config["max_output_tokens"] <= config["context_limit"]
                request_rows.append(
                    {
                        "provider": provider,
                        "model": config["model"],
                        "case": case_name,
                        "arm": arm,
                        "request_id": case["request_id"],
                        "artifact": str(request_path.relative_to(artifacts_dir)),
                        "request_sha256": sha256_bytes(request_path.read_bytes()),
                        "request_bytes": byte_size,
                        "input_tokens_upper": input_tokens,
                        "max_output_tokens": config["max_output_tokens"],
                        "context_limit": config["context_limit"],
                        "fits_context": fits,
                        "max_standard_cost_usd": request_cost(provider, input_tokens),
                    }
                )

    screen = []
    full = []
    for row in request_rows:
        screen.append({"run_id": f"screen--{row['provider']}--{row['case']}--{row['arm']}--1", **row})
    for provider in PROVIDERS:
        for case_name in CASES:
            by_arm = {(row["provider"], row["case"], row["arm"]): row for row in request_rows}
            for repetition in range(1, 6):
                for arm in ("baseline", "candidate"):
                    row = by_arm[(provider, case_name, arm)]
                    full.append({"run_id": f"full--{provider}--{case_name}--{arm}--{repetition}", **row})

    screen_cost = sum(row["max_standard_cost_usd"] for row in screen)
    full_cost = sum(row["max_standard_cost_usd"] for row in full)
    approval_amount = math.ceil(screen_cost)
    plan = {
        "schema": "llm_pilot_plan_v1",
        "behavior_status": "UNVERIFIED_NO_MODEL_CALLED",
        "send_enabled": False,
        "provider_configs": PROVIDERS,
        "prompt_file_sha256": sha256_bytes(PROMPT_PATH.read_bytes()),
        "candidate_system_sha256": sha256_bytes(candidate_system.encode()),
        "canonical_schema_sha256": sha256_bytes(CANONICAL_SCHEMA_PATH.read_bytes()),
        "provider_schema_sha256": sha256_bytes(schema_path.read_bytes()),
        "token_estimate": "ceil(serialized request UTF-8 bytes / 3); replace with provider count endpoint before approval",
        "output_cost_assumption": "every run consumes the full configured 8192 output tokens",
        "views": view_meta,
        "connectivity_screen": {
            "runs": screen,
            "run_count": len(screen),
            "max_standard_cost_usd": screen_cost,
        },
        "full_ab": {
            "runs": full,
            "run_count": len(full),
            "max_standard_cost_usd": full_cost,
        },
        "recommended_maximum_approval_usd": approval_amount,
        "live_protocol": {
            "before_call": "persist the exact request artifact and hash",
            "after_call": "persist the exact response, provider request ID, usage, timing, and hash",
            "order": "alternate baseline and candidate one request at a time",
            "blocking_error": "stop; do not retry a request that may have been charged",
        },
    }
    plan_path = artifacts_dir / "run-plan.json"
    plan_path.write_bytes(json_bytes(plan))
    summary = {
        "raw_files": {name: {key: value for key, value in item.items() if key != "path"} for name, item in verified.items()},
        "views": view_meta,
        "request_count": len(request_rows),
        "connectivity_run_count": len(screen),
        "full_ab_run_count": len(full),
        "connectivity_max_cost_usd": screen_cost,
        "full_ab_max_cost_usd": full_cost,
        "recommended_maximum_approval_usd": approval_amount,
        "plan_sha256": sha256_bytes(plan_path.read_bytes()),
    }
    (artifacts_dir / "build-summary.json").write_bytes(json_bytes(summary))
    return summary


def dry_run(plan_path: Path) -> None:
    plan = json.loads(plan_path.read_text())
    if plan.get("send_enabled") is not False:
        raise ValueError("dry-run refuses a send-enabled plan")
    print(f"behavior={plan['behavior_status']} send_enabled=false")
    print(
        f"connectivity_screen runs={plan['connectivity_screen']['run_count']} "
        f"max_cost_usd={plan['connectivity_screen']['max_standard_cost_usd']:.4f}"
    )
    print(
        f"full_ab runs={plan['full_ab']['run_count']} "
        f"max_cost_usd={plan['full_ab']['max_standard_cost_usd']:.4f} "
        f"approval_ceiling_usd={plan['recommended_maximum_approval_usd']}"
    )
    for row in plan["connectivity_screen"]["runs"]:
        print(
            f"PLAN {row['run_id']} model={row['model']} bytes={row['request_bytes']} "
            f"tokens_upper={row['input_tokens_upper']} fits={str(row['fits_context']).lower()} "
            f"sha256={row['request_sha256']}"
        )


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def check_source_maps(raw_dir: Path, artifacts_dir: Path) -> None:
    marker_pattern = re.compile(r"<S:([^;>]+)(?:;[^>]*)?>")
    for map_path in sorted((artifacts_dir / "views").glob("*.map.json")):
        mapping = json.loads(map_path.read_text())
        view_path = map_path.with_name(map_path.name.replace(".map.json", ".view.txt"))
        view = view_path.read_text()
        emitted = set(marker_pattern.findall(view))
        recorded = set(mapping["source_records"])
        if emitted != recorded:
            raise AssertionError(f"source map mismatch for {view_path.name}")
        raw_text = (raw_dir / mapping["raw_filename"]).read_text()
        if sha256_bytes((raw_dir / mapping["raw_filename"]).read_bytes()) != mapping["raw_sha256"]:
            raise AssertionError(f"raw hash drift for {mapping['raw_filename']}")
        for source_id, record in mapping["source_records"].items():
            start = record["raw_char_start"]
            end = record["raw_char_end"]
            if not (0 <= start <= end <= len(raw_text)):
                raise AssertionError(f"bad raw span for {source_id}")
            if record["kind"] == "html_anchor" and record["anchor"] not in raw_text[start:end]:
                raise AssertionError(f"anchor span does not resolve for {source_id}")


def check_required_evidence(artifacts_dir: Path) -> None:
    requirements = {
        "d101275d20f.htm.view.txt": ["ixv-64800", "ixv-64823", "ixv-64847", "ixv-64868", "ixv-64898", "ixv-64901", "ixv-65207", "u=Unit_USD", "s=3"],
        "cib-20251231.htm.view.txt": ["f-102", "f-128", "f-146", "f-148", "f-249", "u=cop", "s=6"],
        "cni-20251231.htm.view.txt": ["f-95", "f-107", "f-113", "f-125", "f-139", "f-46", "f-290", "u=cad", "s=6"],
        "tm261145d1_40f.htm.view.txt": ["Exhibit", "99.2", "cni-20251231.htm", "incorporated by reference"],
    }
    for filename, needles in requirements.items():
        text = (artifacts_dir / "views" / filename).read_text()
        for needle in needles:
            if needle.casefold() not in text.casefold():
                raise AssertionError(f"required evidence missing from {filename}: {needle}")

    wrapper_request = json.loads((artifacts_dir / "requests/google--cni-wrapper-neighbour--candidate.json").read_text())
    wrapper_text = json.dumps(wrapper_request, ensure_ascii=False)
    exhibit_hash = RAW_FILES["cni-20251231.htm"]["sha256"]
    if exhibit_hash in wrapper_text or "@@DOCUMENT id=E file=cni-20251231.htm" in wrapper_text:
        raise AssertionError("wrapper-only neighbour leaked the exhibit source")
    for request_path in (artifacts_dir / "requests").glob("*.json"):
        text = request_path.read_text()
        for forbidden in ("Gold locations", "expected_outcome", "expected_anchor"):
            if forbidden in text:
                raise AssertionError(f"answer metadata leaked into {request_path.name}")


def check_schema_semantics(artifacts_dir: Path) -> None:
    canonical = json.loads(CANONICAL_SCHEMA_PATH.read_text())
    common = json.loads((artifacts_dir / "provider-common.schema.json").read_text())
    lint_common_schema(common)
    examples = example_candidates()
    for example in examples:
        validate_schema_instance(example, canonical)
        validate_schema_instance(example, common)
    invalid = []
    wrong_outcome = json.loads(json.dumps(examples[0]))
    wrong_outcome["results"][0]["outcome"] = "maybe"
    invalid.append(wrong_outcome)
    missing_found_value = json.loads(json.dumps(examples[0]))
    del missing_found_value["results"][0]["value"]
    invalid.append(missing_found_value)
    missing_checked = json.loads(json.dumps(examples[1]))
    del missing_checked["results"][0]["checked_locations"]
    invalid.append(missing_checked)
    for candidate in invalid:
        for schema in (canonical, common):
            try:
                validate_schema_instance(candidate, schema)
            except SchemaError:
                pass
            else:
                raise AssertionError("invalid outcome shape passed schema validation")


def check_provider_requests(artifacts_dir: Path) -> None:
    common = json.loads((artifacts_dir / "provider-common.schema.json").read_text())
    expected_keys = {
        "openai": {"model", "input", "reasoning", "max_output_tokens", "text", "store", "truncation"},
        "fireworks": {"model", "messages", "response_format", "max_tokens", "stream", "n", "context_length_exceeded_behavior"},
        "google": {"model", "system_instruction", "input", "response_format", "generation_config", "store", "stream"},
        "anthropic": {"model", "max_tokens", "system", "messages", "thinking", "output_config"},
    }
    for provider, config in PROVIDERS.items():
        path = artifacts_dir / "requests" / f"{provider}--aero-primary--candidate.json"
        body = json.loads(path.read_text())
        if set(body) != expected_keys[provider] or body["model"] != config["model"]:
            raise AssertionError(f"unexpected {provider} request fields")
        if provider == "openai":
            assert body["store"] is False and body["truncation"] == "disabled"
            assert body["reasoning"] == {"effort": "none"}
            sent_schema = body["text"]["format"]["schema"]
        elif provider == "fireworks":
            assert body["context_length_exceeded_behavior"] == "error"
            sent_schema = body["response_format"]["json_schema"]["schema"]
        elif provider == "google":
            assert body["store"] is False
            assert body["generation_config"]["thinking_level"] == "minimal"
            sent_schema = body["response_format"]["schema"]
        else:
            assert body["thinking"] == {"type": "disabled"}
            assert body["output_config"]["effort"] == "low"
            sent_schema = body["output_config"]["format"]["schema"]
        if sent_schema != common:
            raise AssertionError(f"{provider} did not receive the common provider schema")


def check_no_secrets(artifacts_dir: Path) -> None:
    patterns = [
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
        re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.I),
    ]
    for path in artifacts_dir.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if any(pattern.search(text) for pattern in patterns):
            raise AssertionError(f"possible secret in {path}")


def self_test(raw_dir: Path, artifacts_dir: Path) -> None:
    verify_raw(raw_dir)
    check_source_maps(raw_dir, artifacts_dir)
    check_required_evidence(artifacts_dir)
    check_schema_semantics(artifacts_dir)
    check_provider_requests(artifacts_dir)
    check_no_secrets(artifacts_dir)
    plan = json.loads((artifacts_dir / "run-plan.json").read_text())
    if plan["connectivity_screen"]["run_count"] != 48 or plan["full_ab"]["run_count"] != 240:
        raise AssertionError("unexpected run count")
    if not all(row["fits_context"] for row in plan["connectivity_screen"]["runs"]):
        raise AssertionError("at least one request exceeds a provider context limit")
    with tempfile.TemporaryDirectory(prefix="llm-pilot-rebuild-") as temporary:
        rebuilt = Path(temporary) / "artifacts"
        build(raw_dir, rebuilt)
        if tree_hashes(rebuilt) != tree_hashes(artifacts_dir):
            raise AssertionError("rebuild hashes differ")
    original_connect = socket.socket.connect
    try:
        def blocked_connect(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("dry-run attempted network access")
        socket.socket.connect = blocked_connect  # type: ignore[method-assign]
        with contextlib.redirect_stdout(io.StringIO()):
            dry_run(artifacts_dir / "run-plan.json")
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
    print("PASS raw_hashes source_maps evidence schema dry_run_no_network secret_scan deterministic_rebuild")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "dry-run", "self-test"))
    parser.add_argument("--raw-dir", type=Path, default=RAW_DEFAULT)
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DEFAULT)
    args = parser.parse_args()
    if args.command == "build":
        print(json.dumps(build(args.raw_dir, args.artifacts_dir), indent=2, sort_keys=True))
    elif args.command == "dry-run":
        dry_run(args.artifacts_dir / "run-plan.json")
    else:
        self_test(args.raw_dir, args.artifacts_dir)


if __name__ == "__main__":
    main()
