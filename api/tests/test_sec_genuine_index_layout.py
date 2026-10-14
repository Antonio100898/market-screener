"""Portable authentic SEC heading/row excerpts; full retained day is a separate control."""
from datetime import date
import pytest
from screener.sec_ingestion import parse_all_form_index, parse_form_index
from test_sec_ingestion import HEADER, _index

# Exact retained9830 physical lines8/9/10/11/132; the excerpt has its own positions.
FIRST_HEADER = 'Form Type   Company Name                                                  CIK'
CONTINUATION = '      Date Filed  File Name'
SEPARATOR = '---------------------------------------------------------------------------------------------------------------------------------------------'
NONFINANCIAL = '1-A         HNO International, Inc.                                       1342916     20230322    edgar/data/1342916/0001262463-23-000015.txt         '
TARGET = '10-K/A      Future FinTech Group Inc.                                     1066923     20230322    edgar/data/1066923/0001213900-23-022083.txt         '


def folded(*rows):
    return (FIRST_HEADER+"\n"+CONTINUATION+"\n"+SEPARATOR+"\n"+"\n".join(rows)+"\n").encode("ascii")


def test_authentic_folded_excerpt_preserves_every_row_and_position():
    rows=parse_all_form_index(folded(NONFINANCIAL,TARGET))
    assert [(row.entry_ordinal,row.physical_line) for row in rows]==[(0,3),(1,4)]
    assert [row.filing.source_row for row in rows]==[NONFINANCIAL,TARGET]
    target=rows[1].filing
    assert (target.form,target.company_name,target.cik,target.filing_date,target.accession,target.archive_filename)==(
        "10-K/A","Future FinTech Group Inc.","0001066923",date(2023,3,22),"0001213900-23-022083",
        "edgar/data/1066923/0001213900-23-022083.txt")
    assert len(target.source_row)==150 and len(target.source_row)-len(target.source_row.rstrip(" "))==9


def test_folded_financial_view_is_original_ordered_projection():
    body=folded(NONFINANCIAL,TARGET)
    rows=parse_all_form_index(body)
    assert tuple(row.filing for row in rows if row.filing.form.startswith(("10-K","10-Q","20-F","40-F","6-K")))==parse_form_index(body)
    assert parse_form_index(body)==(rows[1].filing,)


def test_legacy_layout_and_existing_compact_date_owner_neighbor():
    rows=parse_all_form_index(_index(NONFINANCIAL,TARGET))
    assert [(row.entry_ordinal,row.physical_line) for row in rows]==[(0,4),(1,5)]
    assert [row.filing.source_row for row in rows]==[NONFINANCIAL,TARGET]
    assert rows[1].filing.filing_date==date(2023,3,22)


def test_compact_date_is_already_owned_by_date_fromisoformat():
    assert date.fromisoformat("20230322")==date(2023,3,22)


@pytest.mark.parametrize("body",[
    folded(TARGET)+folded(TARGET),
    _index(TARGET)+_index(TARGET),
    _index(TARGET)+folded(TARGET),
    (FIRST_HEADER+"\n").encode(),
    (FIRST_HEADER+"\n"+CONTINUATION[1:]+"\n"+SEPARATOR+"\n"+TARGET+"\n").encode(),
    (FIRST_HEADER+"\n"+CONTINUATION+"\n"+CONTINUATION+"\n"+SEPARATOR+"\n"+TARGET+"\n").encode(),
    folded(TARGET).replace(SEPARATOR.encode(),b"-"*140),
    folded(TARGET).replace(SEPARATOR.encode(),b"-"*142),
    (HEADER+"\n"+SEPARATOR+"\n"+TARGET+"\n").encode(),
    folded(TARGET).replace(SEPARATOR.encode(),_index(TARGET).splitlines()[3]),
],ids=["duplicate-folded","duplicate-legacy","mixed","truncated","altered-continuation","double-continuation",
       "short-separator","long-separator","legacy-with-contiguous","folded-with-segmented"])
def test_unknown_or_ambiguous_layout_refuses(body):
    with pytest.raises(ValueError,match="header|separator"):
        parse_all_form_index(body)


@pytest.mark.parametrize("row,message",[
    ("too short","short data row"),
    (TARGET[:74]+"bad".ljust(12)+TARGET[86:],"invalid CIK"),
    (TARGET[:86]+"20230230".ljust(12)+TARGET[98:],"invalid filing date"),
    (TARGET.replace("edgar/data/1066923/","edgar/data/9999999/"),"invalid archive filename"),
],ids=["short","CIK","date","archive"])
def test_folded_layout_keeps_original_row_validator(row,message):
    with pytest.raises(ValueError,match=message):
        parse_all_form_index(folded(row))


def test_original_ASCII_admission_remains():
    with pytest.raises(ValueError,match="ASCII"):
        parse_all_form_index(folded(TARGET).replace(b"Future",b"Fut\xffre"))


def test_duplicate_rows_and_blank_physical_lines_are_preserved():
    rows=parse_all_form_index(folded(TARGET,"",TARGET))
    assert [(row.entry_ordinal,row.physical_line) for row in rows]==[(0,3),(1,5)]
    assert rows[0].filing==rows[1].filing and rows[0].filing.source_row==TARGET


def test_folded_layout_retains_exact_row_and_count_frontiers():
    body=folded(NONFINANCIAL,TARGET)
    assert len(parse_all_form_index(body,maximum_rows=2,maximum_row_bytes=150))==2
    with pytest.raises(ValueError,match="row ceiling"):
        parse_all_form_index(body,maximum_rows=1)
    with pytest.raises(ValueError,match="record admission"):
        parse_all_form_index(body,maximum_row_bytes=149)
