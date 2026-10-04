from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from screener.sources.inline_xbrl import (
    InlineXbrlMetadata,
    InlineXbrlPaths,
    UnsupportedInlineXbrlRelationship,
    current_manifest_path,
    current_retained_artifacts,
    incorporated_annual_source,
    manifest_bytes,
    merge_missing_facts,
    parse_inline_xbrl,
    parse_inline_xbrl_bytes,
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


def _current_facts(
    accession="0000000123-26-000001", form="20-F", filed="2026-03-01",
):
    return {"cik": "0000000123", "facts": {"dei": {"EntityPublicFloat": {
        "units": {"USD": [{
            "val": 1, "end": "2025-12-31", "accn": accession,
            "form": form, "filed": filed,
        }]},
    }}}}


def _manifest_fixture(tmp_path: Path, *, incorporated=False):
    cik = "0000000123"
    source_accession = "0000000123-26-000001"
    annual_accession = (
        "0000000123-26-000002" if incorporated else source_accession
    )
    source_directory = tmp_path / "sec-inline" / "accessions" / source_accession.replace("-", "")
    source_directory.mkdir(parents=True)
    metadata, paths, _ = _fixture(source_directory)
    index = source_directory / "index.json"
    index.write_bytes(b'{"source":"index"}')
    source_files = {
        "index": index,
        "instance": paths.instance,
        "filing_summary": paths.filing_summary,
        "presentation": paths.presentation,
        "schema": paths.schema,
        "primary_document": paths.primary_document,
    }

    def record(accession, path):
        raw = path.read_bytes()
        return {
            "document": path.name,
            "url": (
                "https://www.sec.gov/Archives/edgar/data/123/"
                f"{accession.replace('-', '')}/{path.name}"
            ),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }

    relationship_evidence = None
    if incorporated:
        annual_directory = (
            tmp_path / "sec-inline" / "accessions" / annual_accession.replace("-", "")
        )
        annual_directory.mkdir(parents=True)
        annual_index = annual_directory / "index.json"
        annual_index.write_bytes(b'{"annual":"index"}')
        wrapper = annual_directory / "wrapper.htm"
        href = (
            "https://www.sec.gov/Archives/edgar/data/123/"
            f"{source_accession.replace('-', '')}/annual.htm"
        )
        wrapper.write_text(
            "<p>The audited statements are incorporated by reference from "
            "Form 6-K.</p>"
            f'<a href="{href}">Audited annual consolidated financial statements</a>'
        )
        files = {
            "annual_index": record(annual_accession, annual_index),
            "annual_primary_document": record(annual_accession, wrapper),
            "source_index": record(source_accession, index),
            **{
                role: record(source_accession, path)
                for role, path in source_files.items() if role != "index"
            },
        }
        relationship_evidence = incorporated_annual_source(wrapper.read_bytes(), cik)
        relationship = "incorporated_annual_exhibit"
        annual_form = "40-F"
        source_form = "6-K"
        source_filed = "2026-02-28"
        annual_document = wrapper.name
    else:
        files = {
            role: record(annual_accession, path)
            for role, path in source_files.items()
        }
        relationship = "direct_annual"
        annual_form = source_form = "20-F"
        source_filed = "2026-03-01"
        annual_document = paths.primary_document.name

    manifest = {
        "schema": "sec_inline_supplement_v1",
        "parser_contract_revision": 1,
        "relationship": relationship,
        "cik": cik,
        "entity_name": metadata.entity_name,
        "annual": {
            "accession": annual_accession,
            "form": annual_form,
            "filed": "2026-03-01",
            "report_date": "2025-12-31",
            "document": annual_document,
        },
        "source": {
            "accession": source_accession,
            "form": source_form,
            "filed": source_filed,
            "document": paths.primary_document.name,
        },
        "files": files,
    }
    if relationship_evidence is not None:
        manifest["relationship_evidence"] = relationship_evidence
    manifest_path = current_manifest_path(tmp_path, cik)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest_bytes(manifest))
    return manifest, manifest_path


def test_direct_manifest_enumerates_exact_verified_artifacts(tmp_path):
    manifest, manifest_path = _manifest_fixture(tmp_path)

    artifacts = current_retained_artifacts(
        tmp_path, manifest["cik"], _current_facts(),
    )

    assert [artifact.role for artifact in artifacts] == [
        "sec_inline_current_manifest",
        "sec_inline_direct_annual_filing_summary",
        "sec_inline_direct_annual_index",
        "sec_inline_direct_annual_instance",
        "sec_inline_direct_annual_presentation",
        "sec_inline_direct_annual_primary_document",
        "sec_inline_direct_annual_schema",
    ]
    assert artifacts[0].payload == manifest_path.read_bytes()
    for artifact in artifacts:
        assert artifact.payload == artifact.path.read_bytes()
        assert hashlib.sha256(artifact.payload).hexdigest() == artifact.sha256
        assert len(artifact.payload) == artifact.byte_size
        assert str(artifact.path) not in artifact.role


def test_incorporated_manifest_distinguishes_wrapper_and_source_roles(tmp_path):
    manifest, _ = _manifest_fixture(tmp_path, incorporated=True)

    artifacts = current_retained_artifacts(
        tmp_path,
        manifest["cik"],
        _current_facts(
            manifest["annual"]["accession"], "40-F", manifest["annual"]["filed"],
        ),
    )

    assert [artifact.role for artifact in artifacts] == [
        "sec_inline_current_manifest",
        "sec_inline_annual_wrapper_index",
        "sec_inline_annual_wrapper_primary_document",
        "sec_inline_incorporated_source_filing_summary",
        "sec_inline_incorporated_source_index",
        "sec_inline_incorporated_source_instance",
        "sec_inline_incorporated_source_presentation",
        "sec_inline_incorporated_source_primary_document",
        "sec_inline_incorporated_source_schema",
    ]


def test_artifact_enumerator_preserves_two_roles_for_identical_bytes(tmp_path):
    manifest, manifest_path = _manifest_fixture(tmp_path)
    schema = manifest["files"]["schema"]
    manifest["files"]["presentation"] = dict(schema)
    manifest_path.write_bytes(manifest_bytes(manifest))

    artifacts = current_retained_artifacts(tmp_path, manifest["cik"], _current_facts())
    by_role = {artifact.role: artifact for artifact in artifacts}

    presentation = by_role["sec_inline_direct_annual_presentation"]
    schema_artifact = by_role["sec_inline_direct_annual_schema"]
    assert presentation.sha256 == schema_artifact.sha256
    assert presentation.role != schema_artifact.role


@pytest.mark.parametrize(("change", "message"), [
    ("partial", "must name every required file"),
    ("unsafe", "unsafe SEC filing document name"),
    ("tampered", "file verification failed: instance"),
])
def test_artifact_enumerator_rejects_partial_unsafe_or_tampered_manifest(
    tmp_path, change, message,
):
    manifest, manifest_path = _manifest_fixture(tmp_path)
    if change == "partial":
        del manifest["files"]["schema"]
        manifest_path.write_bytes(manifest_bytes(manifest))
    elif change == "unsafe":
        manifest["files"]["schema"]["document"] = "../issuer.xsd"
        manifest_path.write_bytes(manifest_bytes(manifest))
    else:
        record = manifest["files"]["instance"]
        path = (
            tmp_path / "sec-inline" / "accessions"
            / manifest["annual"]["accession"].replace("-", "")
            / record["document"]
        )
        path.write_bytes(b"changed")

    with pytest.raises(ValueError, match=message):
        current_retained_artifacts(tmp_path, manifest["cik"], _current_facts())


def test_artifact_enumerator_rejects_stale_or_duplicate_key_manifest(tmp_path):
    manifest, manifest_path = _manifest_fixture(tmp_path)

    with pytest.raises(ValueError, match="manifest is stale"):
        current_retained_artifacts(
            tmp_path, manifest["cik"], _current_facts("0000000123-26-000099"),
        )

    manifest_path.write_text(
        '{"schema":"sec_inline_supplement_v1",'
        '"schema":"sec_inline_supplement_v1"}'
    )
    with pytest.raises(ValueError, match="invalid retained Inline-XBRL manifest"):
        current_retained_artifacts(tmp_path, manifest["cik"], _current_facts())


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


def test_byte_backed_parser_matches_path_backed_parser(tmp_path):
    metadata, paths, hashes = _fixture(tmp_path)
    payloads = {
        field: getattr(paths, field).read_bytes()
        for field in hashes
    }
    documents = {field: getattr(paths, field).name for field in hashes}

    assert parse_inline_xbrl_bytes(
        metadata, payloads, documents, hashes
    ) == parse_inline_xbrl(metadata, paths, hashes)


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
