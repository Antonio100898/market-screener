from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from screener.sources.inline_xbrl import (
    InlineXbrlMetadata,
    InlineXbrlPaths,
    UnsupportedInlineXbrlRelationship,
    incorporated_annual_source,
    merge_missing_facts,
    parse_inline_xbrl,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _fixture(
    tmp_path: Path,
    *,
    taxonomy: str = "ifrs-full",
    namespace: str = "https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full",
    issuer: str = "0000000123",
    extra_facts: str = "",
    extra_anchors: str = "",
) -> tuple[InlineXbrlMetadata, InlineXbrlPaths, dict[str, str]]:
    instance = _write(tmp_path / "instance_htm.xml", f"""<?xml version="1.0"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:{taxonomy}="{namespace}" xmlns:issuer="https://example.test/issuer">
 <context id="instant"><entity><identifier>{issuer}</identifier></entity>
  <period><instant>2025-12-31</instant></period></context>
 <context id="duration"><entity><identifier>{issuer}</identifier></entity>
  <period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period></context>
 <context id="dimensioned"><entity><identifier>{issuer}</identifier><segment>
  <xbrldi:explicitMember dimension="{taxonomy}:ClassesOfShareCapitalAxis">{taxonomy}:OrdinarySharesMember</xbrldi:explicitMember>
  <xbrldi:typedMember dimension="issuer:RegionAxis"><issuer:RegionDomain>EMEA</issuer:RegionDomain></xbrldi:typedMember>
 </segment></entity><period><instant>2025-12-31</instant></period></context>
 <unit id="USD"><measure>iso4217:USD</measure></unit>
 <unit id="USD-per-share"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>
  <unitDenominator><measure>shares</measure></unitDenominator></divide></unit>
 <{taxonomy}:Assets contextRef="instant" unitRef="USD" decimals="-3" id="asset">1000</{taxonomy}:Assets>
 <{taxonomy}:CashFlowsFromUsedInOperatingActivities contextRef="duration" unitRef="USD" decimals="0" id="cash">-50</{taxonomy}:CashFlowsFromUsedInOperatingActivities>
 <{taxonomy}:EarningsPerShareBasic contextRef="dimensioned" unitRef="USD-per-share" decimals="2" id="eps">1.25</{taxonomy}:EarningsPerShareBasic>
 <issuer:CurrentAssets contextRef="instant" unitRef="USD" decimals="0" id="extension">900</issuer:CurrentAssets>
 {extra_facts}
</xbrl>""")
    summary = _write(tmp_path / "FilingSummary.xml", """<FilingSummary><MyReports><Report>
 <MenuCategory>Statements</MenuCategory><ShortName>Consolidated Balance Sheets</ShortName>
 <Role>role-balance</Role></Report><Report><MenuCategory>Notes</MenuCategory>
 <ShortName>Asset note</ShortName><Role>role-note</Role></Report></MyReports></FilingSummary>""")
    presentation = _write(tmp_path / "issuer_pre.xml", f"""<link:linkbase
 xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
 <link:presentationLink xlink:role="role-balance">
  <link:loc xlink:href="taxonomy.xsd#{taxonomy}_Assets"/>
 </link:presentationLink><link:presentationLink xlink:role="role-note">
  <link:loc xlink:href="taxonomy.xsd#{taxonomy}_Assets"/>
 </link:presentationLink></link:linkbase>""")
    schema = _write(tmp_path / "issuer.xsd", """<xsd:schema
 xmlns:xsd="http://www.w3.org/2001/XMLSchema" targetNamespace="https://example.test/issuer"/>""")
    primary = _write(tmp_path / "annual.htm", f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
 <ix:nonFraction id="asset" name="{taxonomy}:Assets" contextRef="instant" unitRef="USD" decimals="-3" scale="3">1</ix:nonFraction>
 <ix:nonFraction id="cash" name="{taxonomy}:CashFlowsFromUsedInOperatingActivities" contextRef="duration" unitRef="USD" decimals="0" sign="-" scale="0">50</ix:nonFraction>
 <ix:nonFraction id="eps" name="{taxonomy}:EarningsPerShareBasic" contextRef="dimensioned" unitRef="USD-per-share" decimals="2">1.25</ix:nonFraction>
 {extra_anchors}
</body></html>""")
    paths = InlineXbrlPaths(instance, summary, presentation, schema, primary)
    hashes = {
        field: hashlib.sha256(getattr(paths, field).read_bytes()).hexdigest()
        for field in ("instance", "filing_summary", "presentation", "schema", "primary_document")
    }
    metadata = InlineXbrlMetadata(
        cik="123",
        entity_name="Example PLC",
        source_accession="0000000123-26-000001",
        source_form="20-F",
        source_filed="2026-03-01",
        source_document="annual.htm",
        report_date="2025-12-31",
        annual_accession="0000000123-26-000001",
        annual_form="20-F",
        annual_filed="2026-03-01",
    )
    return metadata, paths, hashes


def _entry(payload: dict, namespace: str, concept: str, unit: str) -> dict:
    return payload["facts"][namespace][concept]["units"][unit][0]


def test_ifrs_parser_preserves_values_periods_dimensions_roles_and_sources(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)

    payload = parse_inline_xbrl(metadata, paths, hashes)

    assets = _entry(payload, "ifrs-full", "Assets", "USD")
    assert assets["val"] == "1000"
    assert assets["end"] == "2025-12-31"
    assert "start" not in assets
    assert assets["_source_scale"] == "3"
    assert assets["_source_sign"] is None
    assert assets["_source_statement_roles"] == ["Consolidated Balance Sheets"]
    assert assets["accn"] == metadata.source_accession
    assert assets["form"] == metadata.annual_form
    assert assets["_annual_accession"] == metadata.annual_accession

    cash = _entry(payload, "ifrs-full", "CashFlowsFromUsedInOperatingActivities", "USD")
    assert (cash["start"], cash["end"], cash["val"]) == (
        "2025-01-01", "2025-12-31", "-50",
    )
    assert cash["_source_sign"] == "-"

    eps = _entry(payload, "ifrs-full", "EarningsPerShareBasic", "USD/shares")
    assert eps["segments"].startswith("ifrs-full:ClassesOfShareCapitalAxis=")
    assert eps["_source_dimensions"] == [
        {
            "axis": "ifrs-full:ClassesOfShareCapitalAxis",
            "kind": "explicit",
            "member": "ifrs-full:OrdinarySharesMember",
        },
        {
            "axis": "issuer:RegionAxis",
            "kind": "typed",
            "member": '<ns0:RegionDomain xmlns:ns0="https://example.test/issuer">EMEA</ns0:RegionDomain>',
        },
    ]
    assert "CurrentAssets" not in payload["facts"]["ifrs-full"]
    assert "issuer" not in payload["facts"]
    assert payload["_inline_xbrl"]["standard_fact_count"] == 3
    assert payload["_inline_xbrl"]["files"]["instance"]["sha256"] == hashes["instance"]


def test_us_gaap_parser_keeps_standard_namespace(tmp_path):
    metadata, paths, hashes = _fixture(
        tmp_path,
        taxonomy="us-gaap",
        namespace="http://fasb.org/us-gaap/2025",
    )

    payload = parse_inline_xbrl(metadata, paths, hashes)

    assert set(payload["facts"]) == {"us-gaap"}
    assert _entry(payload, "us-gaap", "Assets", "USD")["_source_namespace_uri"] == (
        "http://fasb.org/us-gaap/2025"
    )


def test_incorporated_facts_select_as_annual_but_keep_exact_source(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    metadata = replace(
        metadata,
        source_accession="0000000123-26-000000",
        source_form="6-K",
        source_filed="2026-02-28",
        source_document="annual.htm",
        annual_accession="0000000123-26-000001",
        annual_form="40-F",
        annual_filed="2026-03-01",
    )

    payload = parse_inline_xbrl(metadata, paths, hashes)
    assets = _entry(payload, "ifrs-full", "Assets", "USD")

    assert (assets["accn"], assets["form"], assets["filed"]) == (
        metadata.annual_accession, metadata.annual_form, metadata.annual_filed,
    )
    assert (
        assets["_source_accession"], assets["_source_form"],
        assets["_source_filed"], assets["_source_document"],
    ) == (
        metadata.source_accession, metadata.source_form,
        metadata.source_filed, metadata.source_document,
    )


def test_incorporated_link_must_be_local_to_its_form_6k_clause():
    payload = (
        '<p>Incorporated by reference from the registrant\'s Form 6-K.</p>'
        + ("<p>Unrelated disclosure.</p>" * 200)
        + '<a href="https://www.sec.gov/Archives/edgar/data/123/'
          '000000012326000009/statements.htm">'
          'Audited annual consolidated financial statements</a>'
    ).encode()

    with pytest.raises(
        UnsupportedInlineXbrlRelationship,
        match="ambiguous_incorporated_statement_links:0",
    ):
        incorporated_annual_source(payload, "123")


def test_compatible_duplicates_choose_most_precise_then_lowest_id(tmp_path):
    metadata, paths, hashes = _fixture(
        tmp_path,
        extra_facts="""
 <ifrs-full:Assets contextRef="instant" unitRef="USD" decimals="0" id="asset10">1001</ifrs-full:Assets>
 <ifrs-full:Assets contextRef="instant" unitRef="USD" decimals="0" id="asset2">1001</ifrs-full:Assets>""",
        extra_anchors="""
 <ix:nonFraction id="asset10" name="ifrs-full:Assets" contextRef="instant" unitRef="USD" decimals="0">1001</ix:nonFraction>
 <ix:nonFraction id="asset2" name="ifrs-full:Assets" contextRef="instant" unitRef="USD" decimals="0">1001</ix:nonFraction>""",
    )

    first = parse_inline_xbrl(metadata, paths, hashes)
    second = parse_inline_xbrl(metadata, paths, hashes)

    assert first == second
    assets = _entry(first, "ifrs-full", "Assets", "USD")
    assert (assets["val"], assets["_source_fact_id"], assets["_source_decimals"]) == (
        "1001", "asset2", "0",
    )


def test_conflicting_duplicate_facts_fail_closed(tmp_path):
    metadata, paths, hashes = _fixture(
        tmp_path,
        extra_facts="""
 <ifrs-full:Assets contextRef="instant" unitRef="USD" decimals="0" id="other">2000</ifrs-full:Assets>""",
        extra_anchors="""
 <ix:nonFraction id="other" name="ifrs-full:Assets" contextRef="instant" unitRef="USD" decimals="0">2000</ix:nonFraction>""",
    )

    with pytest.raises(ValueError, match="conflicting duplicate Inline-XBRL facts"):
        parse_inline_xbrl(metadata, paths, hashes)


def test_wrong_issuer_fails_closed(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path, issuer="0000000999")

    with pytest.raises(ValueError, match="issuer mismatch"):
        parse_inline_xbrl(metadata, paths, hashes)


def test_hash_mismatch_fails_before_parsing(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    hashes["instance"] = "0" * 64

    with pytest.raises(ValueError, match="hash mismatch: instance"):
        parse_inline_xbrl(metadata, paths, hashes)


def test_missing_source_anchor_fails_closed(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    primary = _write(paths.primary_document, paths.primary_document.read_text().replace(
        ' id="asset"', ' id="missing-asset"',
    ))
    paths = replace(paths, primary_document=primary)
    hashes["primary_document"] = hashlib.sha256(primary.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="unresolved Inline-XBRL source anchor: asset"):
        parse_inline_xbrl(metadata, paths, hashes)


@pytest.mark.parametrize(("field", "before", "after", "message"), [
    ("instance", ">1000</ifrs-full:Assets>", ">not-a-number</ifrs-full:Assets>",
     "invalid transformed Inline-XBRL value: asset"),
    ("primary_document", 'scale="3"', 'scale="many"',
     "invalid Inline-XBRL scale: asset"),
])
def test_invalid_numeric_transforms_fail_closed(tmp_path, field, before, after, message):
    metadata, paths, hashes = _fixture(tmp_path)
    path = getattr(paths, field)
    _write(path, path.read_text().replace(before, after))
    hashes[field] = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match=message):
        parse_inline_xbrl(metadata, paths, hashes)


@pytest.mark.parametrize("field", [
    "instance", "filing_summary", "presentation", "schema", "primary_document",
])
def test_every_retained_file_is_required(tmp_path, field):
    metadata, paths, hashes = _fixture(tmp_path)
    missing = (tmp_path / "missing" / "annual.htm" if field == "primary_document"
               else tmp_path / f"missing-{field}")
    paths = replace(paths, **{field: missing})

    with pytest.raises(ValueError, match=f"missing retained Inline-XBRL file: {field}"):
        parse_inline_xbrl(metadata, paths, hashes)


def _companyfacts(entry: dict) -> dict:
    return {
        "cik": "0000000123",
        "entityName": "Example PLC",
        "facts": {"ifrs-full": {"Assets": {"label": "Assets", "units": {
            "USD": [entry],
        }}}},
    }


def test_missing_only_merge_keeps_existing_entry_and_preserves_source_fields(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    supplement = parse_inline_xbrl(metadata, paths, hashes)
    existing = {
        "val": 1000,
        "end": "2025-12-31",
        "accn": metadata.annual_accession,
        "form": "20-F",
        "filed": metadata.annual_filed,
    }
    companyfacts = _companyfacts(existing)

    merged = merge_missing_facts(companyfacts, supplement)

    assets = merged["facts"]["ifrs-full"]["Assets"]
    assert assets["label"] == "Assets"
    assert assets["units"]["USD"] == [existing]
    cash = merged["facts"]["ifrs-full"]["CashFlowsFromUsedInOperatingActivities"] \
        ["units"]["USD"][0]
    assert cash["_source_fact_id"] == "cash"
    assert cash["_source_statement_roles"] == []
    eps = merged["facts"]["ifrs-full"]["EarningsPerShareBasic"] \
        ["units"]["USD/shares"][0]
    assert eps["_source_dimensions"][0]["axis"].endswith("ClassesOfShareCapitalAxis")
    assert companyfacts.get("_inline_xbrl_supplement") is None


def test_missing_only_merge_fails_on_same_filing_context_conflict(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    supplement = parse_inline_xbrl(metadata, paths, hashes)
    companyfacts = _companyfacts({
        "val": 999,
        "end": "2025-12-31",
        "accn": metadata.annual_accession,
        "form": "20-F",
        "filed": metadata.annual_filed,
    })

    with pytest.raises(ValueError, match="conflicting retained Inline-XBRL supplement"):
        merge_missing_facts(companyfacts, supplement)


def test_exact_supplement_duplicates_are_deduplicated(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    supplement = parse_inline_xbrl(metadata, paths, hashes)
    entries = supplement["facts"]["ifrs-full"]["Assets"]["units"]["USD"]
    entries.append(dict(entries[0]))
    companyfacts = {"cik": "0000000123", "facts": {}}

    merged = merge_missing_facts(companyfacts, supplement)

    assert len(merged["facts"]["ifrs-full"]["Assets"]["units"]["USD"]) == 1


def test_incorporated_supplement_rebinds_the_exact_existing_source_fact(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    metadata = replace(
        metadata,
        source_accession="0000000123-26-000000",
        source_form="6-K",
        source_filed="2026-02-28",
        annual_accession="0000000123-26-000001",
        annual_form="40-F",
        annual_filed="2026-03-01",
    )
    supplement = parse_inline_xbrl(metadata, paths, hashes)
    companyfacts = _companyfacts({
        "val": 1000,
        "end": "2025-12-31",
        "accn": metadata.source_accession,
        "form": metadata.source_form,
        "filed": metadata.source_filed,
    })

    merged = merge_missing_facts(companyfacts, supplement)
    entries = merged["facts"]["ifrs-full"]["Assets"]["units"]["USD"]

    assert len(entries) == 1
    assert entries[0]["accn"] == metadata.annual_accession
    assert entries[0]["form"] == metadata.annual_form
    assert entries[0]["_source_accession"] == metadata.source_accession
    assert companyfacts["facts"]["ifrs-full"]["Assets"]["units"]["USD"][0][
        "accn"
    ] == metadata.source_accession
