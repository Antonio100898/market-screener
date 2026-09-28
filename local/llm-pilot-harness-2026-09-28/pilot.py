#!/usr/bin/env python3
"""Build and inspect the frozen SEC extraction pilot without network access."""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
from typing import Any
from urllib import error, request


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROPOSAL = REPO / "local/llm-statement-extraction-proposal-2026-09-28"
RAW_DEFAULT = HERE / "raw"
ARTIFACTS_DEFAULT = HERE / "artifacts"
LIVE_DEFAULT = HERE / "live"
CANONICAL_SCHEMA_PATH = PROPOSAL / "candidate.schema.json"
PROMPT_PATH = PROPOSAL / "prompt.md"
OWNER_LIVE_CAP_USD = Decimal("16")

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

PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

GOLD_FOUND = {
    "aero-primary": {
        "AssetsCurrent": ("1979596", "1000", "USD", "IFRS", "ixv-64800", "instant"),
        "Assets": ("7193091", "1000", "USD", "IFRS", "ixv-64823", "instant"),
        "LiabilitiesCurrent": ("3095938", "1000", "USD", "IFRS", "ixv-64847", "instant"),
        "Liabilities": ("7785307", "1000", "USD", "IFRS", "ixv-64868", "instant"),
        "Equity": ("-592216", "1000", "USD", "IFRS", "ixv-64898", "instant"),
        "EquityAndLiabilities": ("7193091", "1000", "USD", "IFRS", "ixv-64901", "instant"),
        "NetCashProvidedByUsedInOperatingActivities": ("913081", "1000", "USD", "IFRS", "ixv-65207", "duration"),
    },
    "cib-primary": {
        "Assets": ("379752380", "1000000", "COP", "IFRS", "f-102", "instant"),
        "Liabilities": ("338756746", "1000000", "COP", "IFRS", "f-128", "instant"),
        "Equity": ("40995634", "1000000", "COP", "IFRS", "f-146", "instant"),
        "EquityAndLiabilities": ("379752380", "1000000", "COP", "IFRS", "f-148", "instant"),
        "NetIncomeLossAttributableToOwnersOfParent": ("3820634", "1000000", "COP", "IFRS", "f-249", "duration"),
    },
    "cni-exhibit": {
        "AssetsCurrent": ("2471", "1000000", "CAD", "US-GAAP", "f-95", "instant"),
        "Assets": ("58555", "1000000", "CAD", "US-GAAP", "f-107", "instant"),
        "LiabilitiesCurrent": ("3696", "1000000", "CAD", "US-GAAP", "f-113", "instant"),
        "Liabilities": ("36987", "1000000", "CAD", "US-GAAP", "f-125", "instant"),
        "LiabilitiesAndStockholdersEquity": ("58555", "1000000", "CAD", "US-GAAP", "f-139", "instant"),
        "NetIncomeLoss": ("4720", "1000000", "CAD", "US-GAAP", "f-46", "duration"),
        "NetCashProvidedByUsedInOperatingActivities": ("7049", "1000000", "CAD", "US-GAAP", "f-290", "duration"),
    },
    "aero-summary-neighbour": {
        "Assets": ("7193091", "1000", "USD", "IFRS", "ixv-64823", "instant"),
    },
}

GOLD_NOT_FOUND = {
    "cib-unclassified-neighbour": {"AssetsCurrent", "LiabilitiesCurrent"},
    "cni-wrapper-neighbour": {
        "AssetsCurrent",
        "Assets",
        "LiabilitiesCurrent",
        "Liabilities",
        "LiabilitiesAndStockholdersEquity",
        "NetIncomeLoss",
        "NetCashProvidedByUsedInOperatingActivities",
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


class ProviderResponseError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_ledger(path: Path, row: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(json_bytes(row))
        handle.flush()
        os.fsync(handle.fileno())


def live_headers(provider: str, key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "market-screener-llm-pilot/1"}
    if provider in {"openai", "fireworks"}:
        headers["Authorization"] = f"Bearer {key}"
    elif provider == "google":
        headers["x-goog-api-key"] = key
    elif provider == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        raise KeyError(provider)
    return headers


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = {"authorization", "proxy-authorization", "x-api-key", "x-goog-api-key", "cookie", "set-cookie"}
    return {name: "[REDACTED]" if name.casefold() in sensitive else value for name, value in headers.items()}


def scrub_text(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def provider_usage(provider: str, payload: dict[str, Any]) -> tuple[int | None, int | None]:
    if provider == "openai":
        usage = payload.get("usage") or {}
        return usage.get("input_tokens"), usage.get("output_tokens")
    if provider == "fireworks":
        usage = payload.get("usage") or {}
        return usage.get("prompt_tokens"), usage.get("completion_tokens")
    if provider == "google":
        usage = payload.get("usage") or payload.get("usage_metadata") or {}
        return usage.get("input_tokens", usage.get("input_token_count")), usage.get("output_tokens", usage.get("output_token_count"))
    if provider == "anthropic":
        usage = payload.get("usage") or {}
        return usage.get("input_tokens"), usage.get("output_tokens")
    raise KeyError(provider)


def provider_reasoning(provider: str, payload: dict[str, Any]) -> tuple[bool, int]:
    if provider != "fireworks":
        return False, 0
    choices = payload.get("choices") or []
    reasoning = ((choices[0].get("message") or {}).get("reasoning_content") if choices else None) or ""
    return bool(reasoning), len(reasoning)


def adapt_provider_response(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    input_tokens, output_tokens = provider_usage(provider, payload)
    visible_reasoning, reasoning_chars = provider_reasoning(provider, payload)
    if provider == "openai":
        if payload.get("status") != "completed" or payload.get("error") or payload.get("incomplete_details"):
            raise ProviderResponseError(f"OpenAI status={payload.get('status')} error={bool(payload.get('error'))}")
        texts = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    raise ProviderResponseError("OpenAI returned a refusal")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        result = {
            "provider_request_id": payload.get("id"),
            "status": payload.get("status"),
            "finish": payload.get("status"),
            "text": "".join(texts),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    elif provider == "fireworks":
        choices = payload.get("choices") or []
        if not choices or choices[0].get("finish_reason") != "stop":
            raise ProviderResponseError(f"Fireworks finish_reason={choices[0].get('finish_reason') if choices else None}")
        content = (choices[0].get("message") or {}).get("content")
        result = {
            "provider_request_id": payload.get("id"),
            "status": "completed",
            "finish": choices[0].get("finish_reason"),
            "text": content,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    elif provider == "google":
        if payload.get("status") != "completed" or payload.get("errors"):
            raise ProviderResponseError(f"Google status={payload.get('status')} errors={bool(payload.get('errors'))}")
        texts = []
        for step in payload.get("steps", []):
            if step.get("type") != "model_output":
                continue
            for content in step.get("content", []):
                if content.get("type") == "text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        result = {
            "provider_request_id": payload.get("id"),
            "status": payload.get("status"),
            "finish": payload.get("status"),
            "text": "".join(texts),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    elif provider == "anthropic":
        if payload.get("stop_reason") != "end_turn" or payload.get("error"):
            raise ProviderResponseError(f"Anthropic stop_reason={payload.get('stop_reason')} error={bool(payload.get('error'))}")
        texts = [item["text"] for item in payload.get("content", []) if item.get("type") == "text" and isinstance(item.get("text"), str)]
        result = {
            "provider_request_id": payload.get("id"),
            "status": "completed",
            "finish": payload.get("stop_reason"),
            "text": "".join(texts),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    else:
        raise KeyError(provider)
    if not result["text"]:
        raise ProviderResponseError(f"{provider} returned no structured text")
    if not isinstance(result["input_tokens"], int) or not isinstance(result["output_tokens"], int):
        raise ProviderResponseError(f"{provider} response omitted integer token usage")
    result["visible_reasoning"] = visible_reasoning
    result["reasoning_chars"] = reasoning_chars
    return result


def calculated_cost(provider: str, input_tokens: int, output_tokens: int) -> Decimal:
    config = PROVIDERS[provider]
    input_price = Decimal(str(config["input_price"]))
    output_price = Decimal(str(config["output_price"]))
    if provider == "openai" and input_tokens > 272_000:
        input_price *= Decimal("2")
        output_price *= Decimal("1.5")
    return (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal(1_000_000)


def document_for_location(case_name: str, location: dict[str, Any]) -> str | None:
    filename = location.get("document")
    if filename not in CASES[case_name]["documents"]:
        return None
    expected = RAW_FILES[filename]
    if location.get("accession") != expected["accession"] or location.get("document_sha256") != expected["sha256"]:
        return None
    return filename


def normalized_evidence(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", " ", value.casefold()).strip()


def resolve_found_evidence(case_name: str, result: dict[str, Any], artifacts_dir: Path) -> dict[str, bool]:
    location = result.get("location") or {}
    filename = document_for_location(case_name, location)
    if not filename:
        return {"source_resolved": False, "evidence_quote_match": False}
    mapping = json.loads((artifacts_dir / "views" / f"{filename}.map.json").read_text())
    anchor = location.get("html_anchor")
    source_resolved = bool(anchor) and any(
        record.get("kind") == "html_anchor" and record.get("anchor") == anchor
        for record in mapping["source_records"].values()
    )
    view = (artifacts_dir / "views" / f"{filename}.view.txt").read_text()
    plain = re.sub(r"</?T:[^>]+>|<S:[^>]+>|<LINK:[^>]+>|\|", " ", view)
    quote = normalized_evidence(str(result.get("evidence_quote", "")))
    quote_match = bool(quote) and quote in normalized_evidence(plain)
    return {"source_resolved": source_resolved, "evidence_quote_match": quote_match}


def score_candidate(case_name: str, candidate: dict[str, Any], artifacts_dir: Path) -> dict[str, Any]:
    requested = {field["canonical_name"]: field for field in CASES[case_name]["fields"]}
    results = candidate.get("results", []) if isinstance(candidate, dict) else []
    counts: dict[str, int] = {}
    for result in results:
        name = result.get("requested_field") if isinstance(result, dict) else None
        if isinstance(name, str):
            counts[name] = counts.get(name, 0) + 1
    coverage_ok = set(counts) == set(requested) and all(count == 1 for count in counts.values())
    rows = []
    accepted = 0
    found_gold = GOLD_FOUND.get(case_name, {})
    not_found_gold = GOLD_NOT_FOUND.get(case_name, set())
    for result in results:
        if not isinstance(result, dict):
            rows.append({"requested_field": None, "accepted": False, "reason": "result is not an object"})
            continue
        name = result.get("requested_field")
        outcome = result.get("outcome")
        source_checks = {"source_resolved": False, "evidence_quote_match": False}
        reasons = []
        if name not in requested or counts.get(name) != 1:
            reasons.append("field coverage mismatch")
        elif name in found_gold:
            expected_value, scale, currency, basis, anchor, kind = found_gold[name]
            if outcome != "found":
                reasons.append(f"expected found, got {outcome}")
            else:
                source_checks = resolve_found_evidence(case_name, result, artifacts_dir)
                expected_period = requested[name]
                expected = {
                    "value": expected_value,
                    "scale": scale,
                    "unit": currency,
                    "currency": currency,
                    "accounting_basis": basis,
                    "consolidation_scope": "consolidated",
                }
                for key, value in expected.items():
                    if result.get(key) != value:
                        reasons.append(f"{key} mismatch")
                period = result.get("period") or {}
                if period.get("kind") != kind or period.get("start") != expected_period["target_start"] or period.get("end") != expected_period["target_end"]:
                    reasons.append("period mismatch")
                if (result.get("location") or {}).get("html_anchor") != anchor:
                    reasons.append("anchor mismatch")
                if not source_checks["source_resolved"]:
                    reasons.append("source did not resolve")
                if not source_checks["evidence_quote_match"]:
                    reasons.append("evidence quote did not match")
        elif name in not_found_gold:
            if outcome != "not_found":
                reasons.append(f"expected not_found, got {outcome}")
            else:
                checked = result.get("checked_locations") or []
                source_checks["source_resolved"] = bool(checked) and all(
                    document_for_location(case_name, item) is not None for item in checked if isinstance(item, dict)
                ) and all(isinstance(item, dict) for item in checked)
                if not source_checks["source_resolved"]:
                    reasons.append("checked source did not resolve")
        else:
            reasons.append("no frozen expectation")
        ok = not reasons
        accepted += int(ok)
        rows.append({
            "requested_field": name,
            "outcome": outcome,
            "accepted": ok,
            **source_checks,
            "reasons": reasons,
        })
    if not coverage_ok:
        accepted = sum(1 for row in rows if row["accepted"])
    return {
        "requested_fields": len(requested),
        "returned_results": len(results),
        "coverage_ok": coverage_ok,
        "accepted_fields": accepted,
        "failed_fields": len(requested) - accepted,
        "fields": rows,
    }


def validate_live_plan(plan: dict[str, Any], cap_usd: Decimal, artifacts_dir: Path) -> list[dict[str, Any]]:
    if cap_usd <= 0 or cap_usd > OWNER_LIVE_CAP_USD:
        raise ValueError(f"cap must be greater than 0 and at most USD {OWNER_LIVE_CAP_USD}")
    if plan.get("send_enabled") is not False or plan.get("behavior_status") != "UNVERIFIED_NO_MODEL_CALLED":
        raise ValueError("live screen requires the accepted unsent plan")
    if plan.get("prompt_file_sha256") != sha256_bytes(PROMPT_PATH.read_bytes()):
        raise ValueError("approved prompt hash changed")
    if plan.get("canonical_schema_sha256") != sha256_bytes(CANONICAL_SCHEMA_PATH.read_bytes()):
        raise ValueError("canonical schema hash changed")
    provider_schema = artifacts_dir / "provider-common.schema.json"
    if plan.get("provider_schema_sha256") != sha256_bytes(provider_schema.read_bytes()):
        raise ValueError("provider schema hash changed")
    rows = list(plan["connectivity_screen"]["runs"])
    if len(rows) != 48 or not all(row.get("fits_context") for row in rows):
        raise ValueError("live screen plan is incomplete or exceeds context")
    if Decimal(str(sum(row["max_standard_cost_usd"] for row in rows))) > cap_usd:
        raise ValueError("planned maximum cost exceeds cap")
    expected_pairs = {(provider, case, arm) for provider in PROVIDERS for case in CASES for arm in ("baseline", "candidate")}
    if {(row["provider"], row["case"], row["arm"]) for row in rows} != expected_pairs:
        raise ValueError("live screen run set differs from the approved 48 calls")
    for row in rows:
        path = artifacts_dir / row["artifact"]
        if sha256_bytes(path.read_bytes()) != row["request_sha256"]:
            raise ValueError(f"request hash changed: {row['run_id']}")
        body = json.loads(path.read_text())
        if body != provider_request(row["provider"], BASELINE_SYSTEM if row["arm"] == "baseline" else extract_candidate_system(), request_user_text(body, row["provider"]), json.loads(provider_schema.read_text())):
            raise ValueError(f"request configuration changed: {row['run_id']}")
    case_size = {
        case: max(row["request_bytes"] for row in rows if row["case"] == case)
        for case in CASES
    }
    return sorted(rows, key=lambda row: (case_size[row["case"]], list(PROVIDERS).index(row["provider"]), 0 if row["arm"] == "baseline" else 1))


def request_user_text(body: dict[str, Any], provider: str) -> str:
    if provider == "openai":
        return body["input"][1]["content"][0]["text"]
    if provider in {"fireworks", "anthropic"}:
        return body["messages"][-1]["content"]
    if provider == "google":
        return body["input"]
    raise KeyError(provider)


def request_system_text(body: dict[str, Any], provider: str) -> str:
    if provider == "openai":
        return body["input"][0]["content"][0]["text"]
    if provider == "fireworks":
        return body["messages"][0]["content"]
    if provider == "google":
        return body["system_instruction"]
    if provider == "anthropic":
        return body["system"]
    raise KeyError(provider)


def validate_exact_request(row: dict[str, Any], body: dict[str, Any], common_schema: dict[str, Any]) -> None:
    system = BASELINE_SYSTEM if row["arm"] == "baseline" else extract_candidate_system()
    user = request_user_text(body, row["provider"])
    expected = provider_request(row["provider"], system, user, common_schema)
    if body != expected or request_system_text(body, row["provider"]) != system:
        raise ValueError(f"request configuration changed: {row['run_id']}")


def http_post(provider: str, body: bytes, key: str, timeout: int = 900) -> tuple[int, dict[str, str], bytes]:
    headers = live_headers(provider, key)
    req = request.Request(PROVIDERS[provider]["endpoint"], data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def result_divergence(row: dict[str, Any], run_dir: Path) -> str | None:
    if row["state"] == "http_error":
        try:
            payload = json.loads((run_dir / "response.body").read_text())
            message = str((payload.get("error") or {}).get("message", ""))
        except (OSError, json.JSONDecodeError):
            message = ""
        if row["provider"] == "openai":
            limit = re.search(r"Limit (\d+), Requested (\d+)", message)
            return f"HTTP {row['http_status']}: TPM limit {limit.group(1)}, requested {limit.group(2)}" if limit else f"HTTP {row['http_status']}"
        if row["provider"] == "google" and "high demand" in message:
            return f"HTTP {row['http_status']}: model temporarily at high demand"
        if row["provider"] == "anthropic" and "credit balance is too low" in message:
            return f"HTTP {row['http_status']}: credit balance too low"
        return f"HTTP {row['http_status']}"
    if row["state"] != "completed":
        return str(row.get("error") or row["state"])
    if row.get("canonical_schema_ok") is False:
        return f"canonical schema failed: {row.get('canonical_schema_error')}"
    score = row.get("score") or {}
    if score.get("accepted_fields", 0) < score.get("requested_fields", 0):
        reasons = next((field.get("reasons", []) for field in score.get("fields", []) if not field.get("accepted")), [])
        return ", ".join(reasons) or "field extraction failed"
    return None


def reasoning_visibility_note(providers: set[str]) -> str:
    if providers:
        return (
            "Visible reasoning was returned by " + ", ".join(sorted(providers))
            + ". Other provider responses exposed no reasoning text."
        )
    return "No provider response exposed reasoning text."


def write_live_summary(live_dir: Path) -> None:
    results = []
    for path in sorted((live_dir / "runs").glob("*/result.json")):
        results.append(json.loads(path.read_text()))
    results.sort(key=lambda row: row["started_at"])
    providers = {}
    for provider in PROVIDERS:
        rows = [row for row in results if row["provider"] == provider]
        attempted_fields = sum(row.get("requested_field_count", len(CASES[row["case"]]["fields"])) for row in rows)
        completed_fields = sum(len(CASES[row["case"]]["fields"]) for row in rows if row["state"] == "completed")
        accepted_fields = sum((row.get("score") or {}).get("accepted_fields", 0) for row in rows)
        first_divergence_row = next((row for row in rows if result_divergence(row, live_dir / "runs" / row["run_id"])), None)
        providers[provider] = {
            "calls": len(rows),
            "completed": sum(row["state"] == "completed" for row in rows),
            "stopped": sum(row["state"] != "completed" for row in rows),
            "accepted_fields": accepted_fields,
            "failed_fields": completed_fields - accepted_fields,
            "unscored_fields": attempted_fields - completed_fields,
            "requested_fields": attempted_fields,
            "first_divergence": {
                "run_id": first_divergence_row["run_id"],
                "reason": result_divergence(first_divergence_row, live_dir / "runs" / first_divergence_row["run_id"]),
            } if first_divergence_row else None,
            "cost_usd": str(sum((Decimal(row.get("cost_usd", "0")) for row in rows), Decimal("0"))),
        }
    total_cost = sum((Decimal(row.get("cost_usd", "0")) for row in results), Decimal("0"))
    summary = {"calls": len(results), "cost_usd": str(total_cost), "providers": providers}
    (live_dir / "summary.json").write_bytes(json_bytes(summary))
    lines = [
        "# Live connectivity screen",
        "",
        f"Calls with definitive results: {len(results)} of 48. Calculated standard cost: USD {total_cost}.",
        "The screen writes shadow artifacts only. It cannot publish company data.",
        "",
        "| Provider | Calls | Completed | Accepted | Failed | Unscored | Cost USD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for provider, row in providers.items():
        lines.append(f"| {provider} | {row['calls']} | {row['completed']} | {row['accepted_fields']} | {row['failed_fields']} | {row['unscored_fields']} | {row['cost_usd']} |")
    lines.extend(["", "## First divergence or stop", ""])
    for provider, row in providers.items():
        divergence = row["first_divergence"]
        if divergence:
            lines.append(f"- {provider}: `{divergence['run_id']}` — {divergence['reason']}.")
    stopped_rows = [row for row in results if row["state"] != "completed"]
    lines.extend(["", "## Provider stops", ""])
    for row in stopped_rows:
        lines.append(f"- {row['provider']}: `{row['run_id']}` — {result_divergence(row, live_dir / 'runs' / row['run_id'])}. No retry.")
    lines.extend([
        "",
        "This is one sample per arm and case. It proves connectivity only, not a stable model ranking.",
        "Fireworks unexpectedly returned reasoning text despite the structured-output setting; it is retained in the raw response. Other providers exposed no reasoning text.",
    ])
    (live_dir / "README.md").write_text("\n".join(lines) + "\n")
    reasoning_providers = set()
    for row in results:
        response_path = live_dir / "runs" / row["run_id"] / "response.body"
        if not response_path.is_file():
            continue
        payload = json.loads(response_path.read_text())
        if provider_reasoning(row["provider"], payload)[0]:
            reasoning_providers.add(row["provider"])
    reasoning_note = reasoning_visibility_note(reasoning_providers)
    turns = [
        "# Turn table",
        "",
        reasoning_note,
        "",
        "| Run | Model text | Call | Provider answer | Belief after turn |",
        "|---|---|---|---|---|",
    ]
    for row in results:
        run_dir = live_dir / "runs" / row["run_id"]
        parsed_path = run_dir / "parsed.json"
        model_text = parsed_path.read_text().strip()[:400] if parsed_path.is_file() else "No parsed model text."
        belief = "Not inferred; no reasoning was visible."
        response_path = run_dir / "response.body"
        payload = json.loads(response_path.read_text()) if response_path.is_file() else {}
        visible_reasoning, reasoning_chars = provider_reasoning(row["provider"], payload)
        if visible_reasoning:
            reasoning = (((payload.get("choices") or [{}])[0].get("message") or {}).get("reasoning_content") or "")
            model_text = reasoning[:400]
            belief = f"Visible reasoning retained ({reasoning_chars} characters); see the reviewed first divergence in this report."
        model_text = model_text.replace("|", "\\|").replace("\n", " ")
        answer = f"HTTP {row.get('http_status')}; state={row['state']}; finish={row.get('finish')}"
        turns.append(
            f"| `{row['run_id']}` | `{model_text}` | One structured extraction request | {answer} | "
            f"{belief} |"
        )
    (live_dir / "turns.md").write_text("\n".join(turns) + "\n")
    run_lines = [
        "# Per-run results",
        "",
        "| Provider | Case | Arm | State | Accepted | Failed | Unscored | Input tokens | Output tokens | Cost USD |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        score = row.get("score") or {}
        requested_fields = len(CASES[row["case"]]["fields"])
        accepted = score.get("accepted_fields", 0)
        failed = requested_fields - accepted if row["state"] == "completed" else 0
        unscored = requested_fields if row["state"] != "completed" else 0
        run_lines.append(
            f"| {row['provider']} | {row['case']} | {row['arm']} | {row['state']} | {accepted} | {failed} | {unscored} | "
            f"{row.get('input_tokens', '')} | {row.get('output_tokens', '')} | {row.get('cost_usd', '0')} |"
        )
    (live_dir / "results.md").write_text("\n".join(run_lines) + "\n")


def live_screen(artifacts_dir: Path, live_dir: Path, cap_usd: Decimal, live: bool) -> None:
    if not live:
        raise ValueError("live-screen requires --live")
    if live_dir.exists() and any(live_dir.iterdir()):
        raise ValueError(f"live directory is not empty; refusing a possible duplicate: {live_dir}")
    plan = json.loads((artifacts_dir / "run-plan.json").read_text())
    ordered = validate_live_plan(plan, cap_usd, artifacts_dir)
    common_schema = json.loads((artifacts_dir / "provider-common.schema.json").read_text())
    canonical_schema = json.loads(CANONICAL_SCHEMA_PATH.read_text())
    keys = {}
    missing = []
    for provider, variable in PROVIDER_KEYS.items():
        value = os.environ.get(variable)
        if not value:
            missing.append(variable)
        else:
            keys[provider] = value
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")

    live_dir.mkdir(parents=True)
    (live_dir / "runs").mkdir()
    ledger = live_dir / "ledger.jsonl"
    append_ledger(ledger, {
        "event": "run_started",
        "at": utc_now(),
        "cap_usd": str(cap_usd),
        "planned_calls": len(ordered),
        "planned_max_cost_usd": str(plan["connectivity_screen"]["max_standard_cost_usd"]),
    })
    observed_cost = Decimal("0")
    stopped_providers: set[str] = set()
    remaining = list(ordered)
    secrets = list(keys.values())
    for index, row in enumerate(ordered):
        remaining = ordered[index:]
        if row["provider"] in stopped_providers:
            continue
        projected = observed_cost + sum((Decimal(str(item["max_standard_cost_usd"])) for item in remaining if item["provider"] not in stopped_providers), Decimal("0"))
        if projected > cap_usd or observed_cost > cap_usd:
            append_ledger(ledger, {"event": "cap_stop", "at": utc_now(), "observed_cost_usd": str(observed_cost), "projected_cost_usd": str(projected)})
            break
        provider = row["provider"]
        request_path = artifacts_dir / row["artifact"]
        body = request_path.read_bytes()
        parsed_body = json.loads(body)
        validate_exact_request(row, parsed_body, common_schema)
        run_dir = live_dir / "runs" / row["run_id"]
        run_dir.mkdir()
        headers = live_headers(provider, keys[provider])
        (run_dir / "request.body").write_bytes(body)
        (run_dir / "request.headers.json").write_bytes(json_bytes(redact_headers(headers)))
        (run_dir / "request.meta.json").write_bytes(json_bytes({
            "url": PROVIDERS[provider]["endpoint"],
            "request_sha256": sha256_bytes(body),
        }))
        (run_dir / "request.json").write_bytes(json_bytes({
            "url": PROVIDERS[provider]["endpoint"],
            "headers": redact_headers(headers),
            "body": parsed_body,
            "request_sha256": sha256_bytes(body),
        }))
        append_ledger(ledger, {"event": "prepared", "at": utc_now(), "run_id": row["run_id"], "request_sha256": sha256_bytes(body)})
        append_ledger(ledger, {"event": "sending", "at": utc_now(), "run_id": row["run_id"]})
        started = time.monotonic()
        started_at = utc_now()
        try:
            status, response_headers, response_body = http_post(provider, body, keys[provider])
        except Exception as exc:
            ended_at = utc_now()
            failure = {
                "run_id": row["run_id"], "provider": provider, "model": row["model"], "case": row["case"], "arm": row["arm"],
                "state": "uncertain_transport_failure", "started_at": started_at, "ended_at": ended_at,
                "latency_seconds": round(time.monotonic() - started, 6),
                "error_type": type(exc).__name__, "error": scrub_text(str(exc), secrets), "cost_usd": "0",
            }
            (run_dir / "result.json").write_bytes(json_bytes(failure))
            append_ledger(ledger, {"event": "uncertain_transport_failure", "at": ended_at, "run_id": row["run_id"], "error_type": type(exc).__name__})
            stopped_providers.add(provider)
            write_live_summary(live_dir)
            print(f"STOP {provider} {row['run_id']} uncertain transport failure", flush=True)
            continue
        ended_at = utc_now()
        latency = round(time.monotonic() - started, 6)
        redacted_response_headers = redact_headers(response_headers)
        (run_dir / "response.headers.json").write_bytes(json_bytes(redacted_response_headers))
        (run_dir / "response.body").write_bytes(response_body)
        append_ledger(ledger, {"event": "received", "at": ended_at, "run_id": row["run_id"], "http_status": status, "response_sha256": sha256_bytes(response_body)})
        base_result = {
            "run_id": row["run_id"], "provider": provider, "model": row["model"], "case": row["case"], "arm": row["arm"],
            "started_at": started_at, "ended_at": ended_at, "latency_seconds": latency, "http_status": status,
            "response_sha256": sha256_bytes(response_body), "visible_reasoning": False,
            "requested_field_count": len(CASES[row["case"]]["fields"]),
        }
        if not 200 <= status < 300:
            result = {**base_result, "state": "http_error", "cost_usd": "0"}
            (run_dir / "result.json").write_bytes(json_bytes(result))
            stopped_providers.add(provider)
            append_ledger(ledger, {"event": "provider_stopped", "at": utc_now(), "run_id": row["run_id"], "reason": f"HTTP {status}"})
            write_live_summary(live_dir)
            print(f"STOP {provider} {row['run_id']} HTTP {status}", flush=True)
            continue
        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(response_body)
            adapted = adapt_provider_response(provider, payload)
            candidate = json.loads(adapted["text"])
            validate_schema_instance(candidate, common_schema)
        except (json.JSONDecodeError, SchemaError, ProviderResponseError, TypeError, UnicodeDecodeError) as exc:
            input_tokens = output_tokens = None
            if isinstance(payload, dict):
                input_tokens, output_tokens = provider_usage(provider, payload)
            cost = calculated_cost(provider, input_tokens, output_tokens) if isinstance(input_tokens, int) and isinstance(output_tokens, int) else Decimal("0")
            observed_cost += cost
            result = {
                **base_result, "state": "provider_shape_error", "cost_usd": str(cost),
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "visible_reasoning": provider_reasoning(provider, payload)[0] if isinstance(payload, dict) else False,
                "reasoning_chars": provider_reasoning(provider, payload)[1] if isinstance(payload, dict) else 0,
                "error": scrub_text(str(exc), secrets),
            }
            (run_dir / "result.json").write_bytes(json_bytes(result))
            stopped_providers.add(provider)
            append_ledger(ledger, {"event": "provider_stopped", "at": utc_now(), "run_id": row["run_id"], "reason": type(exc).__name__})
            write_live_summary(live_dir)
            print(f"STOP {provider} {row['run_id']} response/config shape error", flush=True)
            continue
        cost = calculated_cost(provider, adapted["input_tokens"], adapted["output_tokens"])
        observed_cost += cost
        canonical_ok = True
        canonical_error = None
        try:
            validate_schema_instance(candidate, canonical_schema)
        except SchemaError as exc:
            canonical_ok = False
            canonical_error = str(exc)
        score = score_candidate(row["case"], candidate, artifacts_dir) if canonical_ok else None
        (run_dir / "parsed.json").write_bytes(json_bytes(candidate))
        result = {
            **base_result,
            "state": "completed",
            "provider_request_id": adapted["provider_request_id"],
            "finish": adapted["finish"],
            "input_tokens": adapted["input_tokens"],
            "output_tokens": adapted["output_tokens"],
            "cost_usd": str(cost),
            "canonical_schema_ok": canonical_ok,
            "canonical_schema_error": canonical_error,
            "visible_reasoning": adapted["visible_reasoning"],
            "reasoning_chars": adapted["reasoning_chars"],
            "score": score,
        }
        (run_dir / "result.json").write_bytes(json_bytes(result))
        append_ledger(ledger, {"event": "completed", "at": utc_now(), "run_id": row["run_id"], "provider_request_id": adapted["provider_request_id"], "cost_usd": str(cost), "canonical_schema_ok": canonical_ok})
        write_live_summary(live_dir)
        accepted = score["accepted_fields"] if score else 0
        requested_count = score["requested_fields"] if score else len(CASES[row["case"]]["fields"])
        print(f"DONE {row['run_id']} accepted={accepted}/{requested_count} cost_usd={cost}", flush=True)
    append_ledger(ledger, {"event": "run_finished", "at": utc_now(), "observed_cost_usd": str(observed_cost), "stopped_providers": sorted(stopped_providers)})
    write_live_summary(live_dir)


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


def check_live_protocol(artifacts_dir: Path) -> None:
    sample_text = json.dumps(example_candidates()[0])
    bodies = {
        "openai": {
            "id": "resp-test", "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"content": [{"type": "output_text", "text": sample_text}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        "fireworks": {
            "id": "resp-test", "choices": [{"finish_reason": "stop", "message": {"content": sample_text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        "google": {
            "id": "resp-test", "status": "completed", "errors": [],
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": sample_text}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        "anthropic": {
            "id": "resp-test", "stop_reason": "end_turn", "content": [{"type": "text", "text": sample_text}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    for provider, body in bodies.items():
        if provider == "fireworks":
            body["choices"][0]["message"]["reasoning_content"] = "Visible provider reasoning."
        adapted = adapt_provider_response(provider, body)
        if adapted["text"] != sample_text or adapted["input_tokens"] != 10 or adapted["output_tokens"] != 5:
            raise AssertionError(f"{provider} response adapter mismatch")
        if provider == "fireworks":
            if adapted["visible_reasoning"] is not True or adapted["reasoning_chars"] != 27:
                raise AssertionError("Fireworks visible reasoning was not recorded")
        elif adapted["visible_reasoning"] is not False or adapted["reasoning_chars"] != 0:
            raise AssertionError(f"{provider} invented visible reasoning")
    if reasoning_visibility_note({"fireworks"}) != (
        "Visible reasoning was returned by fireworks. Other provider responses exposed no reasoning text."
    ):
        raise AssertionError("visible-reasoning report text regressed")

    secret = "unit-test-secret-value"
    headers = redact_headers(live_headers("openai", secret))
    if secret in json.dumps(headers) or headers["Authorization"] != "[REDACTED]":
        raise AssertionError("credential header was not redacted")
    if secret in scrub_text(f"failed with {secret}", [secret]):
        raise AssertionError("exception text was not redacted")

    plan = json.loads((artifacts_dir / "run-plan.json").read_text())
    ordered = validate_live_plan(plan, OWNER_LIVE_CAP_USD, artifacts_dir)
    if len(ordered) != 48:
        raise AssertionError("live plan did not validate 48 calls")
    try:
        validate_live_plan(plan, OWNER_LIVE_CAP_USD + Decimal("0.01"), artifacts_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("cap above owner approval was accepted")

    gold = {
        "schema": "statement_candidate_v1",
        "request_id": "pilot-aero-summary-neighbour-v1",
        "results": [{
            "requested_field": "Assets", "outcome": "found", "value": "7193091", "scale": "1000",
            "source_label": "Total assets", "unit": "USD", "currency": "USD",
            "period": {"kind": "instant", "start": None, "end": "2025-12-31"},
            "accounting_basis": "IFRS", "consolidation_scope": "consolidated",
            "location": {
                "accession": RAW_FILES["d101275d20f.htm"]["accession"],
                "document": "d101275d20f.htm",
                "document_sha256": RAW_FILES["d101275d20f.htm"]["sha256"],
                "html_anchor": "ixv-64823", "page": None,
                "table": "Consolidated statements of financial position", "row": "Total assets", "column": "2025",
            },
            "evidence_quote": "Total assets $ 7,193,091",
        }],
    }
    score = score_candidate("aero-summary-neighbour", gold, artifacts_dir)
    if score["accepted_fields"] != 1:
        raise AssertionError(f"known citation did not pass: {score}")
    gold["results"][0]["location"]["html_anchor"] = "wrong-anchor"
    if score_candidate("aero-summary-neighbour", gold, artifacts_dir)["accepted_fields"] != 0:
        raise AssertionError("wrong citation was accepted")

    with tempfile.TemporaryDirectory(prefix="llm-pilot-live-protocol-") as temporary:
        temporary_path = Path(temporary)
        ledger = temporary_path / "ledger.jsonl"
        append_ledger(ledger, {"event": "prepared", "run_id": "test"})
        append_ledger(ledger, {"event": "sending", "run_id": "test"})
        events = [json.loads(line)["event"] for line in ledger.read_text().splitlines()]
        if events != ["prepared", "sending"]:
            raise AssertionError("pre-send ledger order changed")
        try:
            live_screen(artifacts_dir, temporary_path, OWNER_LIVE_CAP_USD, True)
        except ValueError as exc:
            if "possible duplicate" not in str(exc):
                raise
        else:
            raise AssertionError("restart did not refuse an uncertain duplicate")


def self_test(raw_dir: Path, artifacts_dir: Path) -> None:
    verify_raw(raw_dir)
    check_source_maps(raw_dir, artifacts_dir)
    check_required_evidence(artifacts_dir)
    check_schema_semantics(artifacts_dir)
    check_provider_requests(artifacts_dir)
    check_no_secrets(artifacts_dir)
    check_live_protocol(artifacts_dir)
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
    print("PASS raw_hashes source_maps evidence schema live_protocol dry_run_no_network secret_scan deterministic_rebuild")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "dry-run", "self-test", "live-screen"))
    parser.add_argument("--raw-dir", type=Path, default=RAW_DEFAULT)
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DEFAULT)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DEFAULT)
    parser.add_argument("--cap-usd", type=Decimal)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        print(json.dumps(build(args.raw_dir, args.artifacts_dir), indent=2, sort_keys=True))
    elif args.command == "dry-run":
        dry_run(args.artifacts_dir / "run-plan.json")
    elif args.command == "self-test":
        self_test(args.raw_dir, args.artifacts_dir)
    else:
        if args.cap_usd is None:
            parser.error("live-screen requires --cap-usd")
        live_screen(args.artifacts_dir, args.live_dir, args.cap_usd, args.live)


if __name__ == "__main__":
    main()
