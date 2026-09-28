"""SEC cover identity parsing and class policy."""

from decimal import Decimal
from pathlib import Path

import pytest

from screener.sources import cover


def _table(*rows):
    return "<table>" + "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    ) + "</table>"


def test_cover_report_cache_path_keeps_accession_and_report_identity(tmp_path):
    assert cover.report_cache_path(
        tmp_path, "0001104659-26-043468", 2
    ) == Path(tmp_path, "covers", "000110465926043468", "R2.htm")


def test_primary_document_cache_path_stays_under_the_accession(tmp_path):
    assert cover.primary_document_cache_path(
        tmp_path, "0001104659-26-043468", "issuer-20251231.htm"
    ) == Path(
        tmp_path,
        "covers",
        "000110465926043468",
        "primary",
        "issuer-20251231.htm",
    )

    with pytest.raises(ValueError, match="primary document name"):
        cover.primary_document_cache_path(
            tmp_path, "0001104659-26-043468", "../other.htm"
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "American Depositary Shares, each representing four (4) ordinary shares",
            Decimal("4"),
        ),
        (
            "American Depositary Shares, each representing one-tenth of one ordinary share",
            Decimal("0.1"),
        ),
        (
            "American depositary shares, every 3 of which represent 40 Class A common shares",
            Decimal(40) / Decimal(3),
        ),
        (
            "American Depositary Shares, each representing one half of one ordinary share",
            Decimal("0.5"),
        ),
        (
            "American depositary shares, each six (6) ADSs representing one (1) "
            "Class A ordinary share",
            Decimal(1) / Decimal(6),
        ),
        (
            "American depositary shares, each ADS representing one and one quarter "
            "ordinary shares",
            Decimal("1.25"),
        ),
        (
            "American depositary shares, with every 13 ADSs representing 10 "
            "Class A ordinary shares",
            Decimal(10) / Decimal(13),
        ),
        ("Where ADSs are held, two ADSs represent one share", Decimal("0.5")),
        (
            "ADSs, representing an equal number of Ordinary Shares, are traded on NYSE",
            Decimal("1"),
        ),
        (
            "The Class A American Depositary Shares each represent one Class A Ordinary Share",
            Decimal("1"),
        ),
        (
            "Our ADSs have been listed since 2003, each representing one common "
            "ordinary share",
            Decimal("1"),
        ),
        (
            "American Depositary Shares, each representing half a Class B Share",
            Decimal("0.5"),
        ),
        (
            "American Depositary Shares, every threerepresenting two Class A "
            "ordinary shares",
            Decimal(2) / Decimal(3),
        ),
        (
            "American Depositary Shares, each representing five thousand Ordinary Shares",
            Decimal("5000"),
        ),
        (
            "American Depositary Shares(each representing one RELX PLC ordinary share)",
            Decimal("1"),
        ),
        (
            "American Depositary Shares, or ADSs,** each representing one of our "
            "common shares",
            Decimal("1"),
        ),
        (
            "American depositary shares (one American depositary share representing "
            "twenty Class A Ordinary Shares)",
            Decimal("20"),
        ),
        (
            "American Depositary Shares, each representing five ordinary shares",
            Decimal("5"),
        ),
        (
            "American Depositary Shares, each representing eight ordinary shares",
            Decimal("8"),
        ),
        (
            "American Depositary Shares. Each receipt represents ten shares of "
            "common stock.",
            Decimal("10"),
        ),
    ],
)
def test_direct_depositary_relations_return_exact_decimals(text, expected):
    assert cover.depositary_ratio(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "American Depositary Shares, each representing 10 BD units",
        "American Depositary Shares, each representing 10 units",
        "American Depositary Shares, each representing one preferred share",
        (
            "Each ADS represents ten CPOs and each CPO represents one Series A share. "
            "Our ADSs, each representing 10 of our Series A shares, are listed on NYSE."
        ),
        "American Depositary Shares, each representing zero ordinary shares",
        "American Depositary Shares, each representing -2 ordinary shares",
        "Common Stock, $0.25 Par Value",
    ],
)
def test_non_direct_or_nonpositive_relations_stay_unresolved(text):
    assert cover.depositary_ratio(text) is None


def test_all_direct_relations_must_agree():
    assert cover.depositary_ratio(
        "Two ADSs represent one ordinary share. Each ADS represents one half "
        "of one ordinary share."
    ) == Decimal("0.5")
    assert cover.depositary_ratio(
        "Each ADS represents eight common shares. Historical ADSs each represented "
        "one common share."
    ) is None


def test_cover_keeps_continued_title_with_its_symbol_and_not_the_next_class():
    page = _table(
        ("Security 12b Title", "American depositary"),
        ("", "shares"),
        ("Trading Symbol", "ADAG"),
        ("Security 12b Title", "5.00% Notes due 2030"),
        ("Trading Symbol", "ADAG30"),
    )

    assert cover.securities(page) == [
        {"title": "American depositary shares", "symbol": "ADAG"},
        {"title": "5.00% Notes due 2030", "symbol": "ADAG30"},
    ]


def test_parallel_cover_columns_keep_blank_positions_from_cross_pairing():
    page = _table(
        ("Security 12b Title", "Common Shares", "", "5.00% Notes due 2030"),
        ("Trading Symbol", "GOOD", "false", "DEBT30"),
    )

    assert cover.securities(page) == [
        {"title": "Common Shares", "symbol": "GOOD"},
        {"title": "5.00% Notes due 2030", "symbol": "DEBT30"},
    ]


def test_cover_rejects_no_symbol_flags_without_losing_real_classes():
    page = _table(
        ("Title of 12(b) Security", "Class A Ordinary shares"),
        ("No Trading Symbol Flag", "true"),
        ("Security 12(b) Title", "Common Shares"),
        ("No Trading Symbol Flag", "F"),
        ("Security 12 b", "Limited Partnership Units"),
        ("Trading Symbol", "BEP"),
    )

    assert cover.securities(page) == [
        {"title": "Limited Partnership Units", "symbol": "BEP"},
    ]
    assert cover.is_common_equity_security("Limited Partnership Units") is True


def test_cover_keeps_one_compound_ads_class_and_matches_its_exact_component():
    page = _table(
        ("Title of 12(b) Security",
         "American Depositary Shares, each representing one common share"),
        ("Trading Symbol", "SUZB3/SUZ"),
        ("Title of 12(b) Security", "6.000% Notes due 2029"),
        ("Trading Symbol", "SUZ/29"),
    )

    assert cover.securities(page) == [
        {"title": "American Depositary Shares, each representing one common share",
         "symbol": "SUZB3/SUZ"},
        {"title": "6.000% Notes due 2029", "symbol": "SUZ/29"},
    ]
    assert cover.symbol_matches(
        "SUZB3/SUZ", "American Depositary Shares", "SUZ"
    ) is True
    assert cover.symbol_matches("SUZ/29", "6.000% Notes due 2029", "SUZ") is False


def test_common_class_with_attached_rights_is_not_a_rights_security():
    accepted = (
        "American Depositary Shares, each representing the right to receive one ordinary share, "
        "par value €0.49 per share",
        "Common Stock, including the Preferred Stock Purchase Rights",
        "Common Shares, including associated Preferred Share Purchase Rights under the agreement",
        "Common Shares, $0.001 par value, including associated Share Purchase Rights under the "
        "Shareholder Protection Rights Agreement",
        "Common shares, no par value (together with associatedcommon share purchase rights)",
        "Common shares (including common share purchase rights)",
    )
    rejected = (
        "Rights to purchase Common Shares",
        "Subscription Rights to purchase Ordinary Shares",
        "Share Purchase Rights",
        "Common Share Purchase Rights",
        "Preferred Shares, together with associated common share purchase rights",
        "Common Shares, including common share purchase rights and warrants",
        "Warrants to Purchase Common Stock",
        "Preferred shares",
        "6.000% Notes due 2029",
        "Gold Shares ETNs due 2033",
    )

    assert all(cover.is_common_equity_security(title) for title in accepted)
    assert not any(cover.is_common_equity_security(title) for title in rejected)


def test_symbol_before_title_pairs_adag_and_drops_aure_placeholder():
    page = _table(
        ("Trading Symbol", "ADAG"),
        ("Security Exchange Name", "NASDAQ"),
        ("Title of 12(b) Security", "American depositary shares"),
        ("Security Exchange Name", "NASDAQ"),
        ("No Trading Symbol Flag", "true"),
        ("Title of 12(b) Security", "Class A Ordinary shares"),
    )

    assert cover.securities(page) == [
        {"title": "American depositary shares", "symbol": "ADAG"},
    ]


def test_azn_placeholder_does_not_pair_with_the_next_debt_symbol():
    page = _table(
        ("Title of 12(b) Security", "Ordinary Shares of 25¢ each"),
        ("No Trading Symbol Flag", "true"),
        ("Title of 12(b) Security", "0.700% Notes due 2026"),
        ("Trading Symbol", "AZN 26"),
    )

    assert cover.securities(page) == [
        {"title": "0.700% Notes due 2026", "symbol": "AZN 26"},
    ]
