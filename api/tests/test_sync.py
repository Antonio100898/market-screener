"""Store and price-application tests — no network."""
from dataclasses import replace
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from screener import store, sync
from screener.models import Fact, PriceHistory, Provenance, Quote
from screener.sources import cover, inline_xbrl
from screener.sources.prices import YahooPriceProvider
from screener.sync import (apply_price, material_events, _restate_historical_ratios,
                           _statement_source_namespace, _validated_price_history)

from tests.helpers import build


def _provenance_leaf(tag: str, annual: bool) -> Provenance:
    return Provenance(
        concept=tag,
        tag=f"us-gaap:{tag}",
        fiscal_year=2025,
        form="6-K",
        accession="0000000123-26-000009",
        filed=date(2026, 2, 28),
        period_end=date(2025, 12, 31),
        annual_accession="0000000123-26-000010" if annual else None,
        annual_form="40-F" if annual else None,
        annual_filed=date(2026, 3, 1) if annual else None,
    )


def test_uniform_derived_provenance_serializes_the_shared_annual_relationship():
    left = _provenance_leaf("Assets", True)
    right = _provenance_leaf("Equity", True)
    fact = Fact(Decimal("1"), replace(
        left, tag="us-gaap:Assets - us-gaap:Equity", components=(left, right),
    ))

    source = sync._source(fact)

    assert source["annual_accn"] == "0000000123-26-000010"
    assert all(component["annual_accn"] == "0000000123-26-000010"
               for component in source["components"])


def test_mixed_derived_provenance_does_not_claim_one_annual_relationship():
    bound = _provenance_leaf("Assets", True)
    unbound = _provenance_leaf("Equity", False)
    fact = Fact(Decimal("1"), replace(
        bound, tag="us-gaap:Assets - us-gaap:Equity", components=(bound, unbound),
    ))

    source = sync._source(fact)

    assert "annual_accn" not in source
    assert source["components"][0]["annual_accn"] == "0000000123-26-000010"
    assert "annual_accn" not in source["components"][1]


def row(ttm=5.0, tbvps=10.0, others="PASS"):
    return {
        "ttm_eps": ttm, "tbvps": tbvps,
        "criteria": [
            {"n": 1, "status": "INSUFFICIENT_DATA", "value": None, "note": "no price"},
            *[{"n": n, "status": others, "value": None, "note": None} for n in (2, 3, 4, 5)],
            {"n": 7, "status": "INSUFFICIENT_DATA", "value": None, "note": "no price"},
        ],
    }


def test_receipt_rebases_historical_per_share_books_only():
    ratios = {2025: {"bvps": 4.0, "tbvps": 3.0, "ncavps": 1.0,
                     "return_on_book": 12.0, "award_pct": 5.0}}
    _restate_historical_ratios(ratios, Decimal("3"))
    assert ratios[2025] == {"bvps": 12.0, "tbvps": 9.0, "ncavps": 3.0,
                            "return_on_book": 12.0, "award_pct": 5.0}


def test_fractional_receipt_rebases_historical_books_too():
    ratios = {2025: {"bvps": 4.0, "tbvps": 3.0, "ncavps": 1.0}}
    _restate_historical_ratios(ratios, Decimal("0.4"))
    assert ratios[2025] == pytest.approx({"bvps": 1.6, "tbvps": 1.2, "ncavps": 0.4})


def test_statement_namespace_survives_a_withheld_ifrs_asset_total():
    snap = build()
    ifrs_liabilities = replace(
        snap.total_liabilities,
        provenance=replace(
            snap.total_liabilities.provenance,
            tag="ifrs-full:Liabilities",
        ),
    )
    snap = replace(snap, total_assets=None, total_liabilities=ifrs_liabilities)

    assert _statement_source_namespace(snap) == "ifrs-full"


def test_export_strip_keeps_operating_returns_but_moves_evidence_to_detail():
    payload = {"annual_ratios": {2025: {
        "nopat": 75.0,
        "ronta": 30.0,
        "lease_neutral_ronta": 35.0,
        "operating_return_assumptions": ["intangibles"],
        "operating_return_caveats": ["disclosed"],
        "operating_return_evidence": {"operating_income": [["tag"]]},
    }}, "operating_returns_estimate": {
        "status": "CONSERVATIVE_LOWER_BOUND",
        "ronta": 25.0,
        "operating_return_assumptions": ["intangibles"],
        "operating_return_evidence": {"operating_income": [["tag"]]},
    }}

    sync._strip_detail_only_evidence(payload)

    assert payload["annual_ratios"][2025] == {
        "nopat": 75.0,
        "ronta": 30.0,
        "lease_neutral_ronta": 35.0,
        "operating_return_assumptions": ["intangibles"],
        "operating_return_caveats": ["disclosed"],
    }
    assert payload["operating_returns_estimate"] == {
        "status": "CONSERVATIVE_LOWER_BOUND",
        "ronta": 25.0,
        "operating_return_assumptions": ["intangibles"],
    }


def test_return_quality_assumption_serializes_only_ranking_inputs_and_disclosure():
    assumed_row = {
        "profitability": {"on_equity": 30.0},
        "operating_returns": {
            "nopat_roic": 20.0,
            "ronta": 40.0,
            "operating_return_assumptions": ["intangibles", "cash"],
        },
        "debt_to_equity": 0.0,
        "assumptions": ["debt", "goodwill"],
    }

    payload = sync._return_quality_assumption(assumed_row)

    assert payload["status"] == "APPLIED"
    assert payload["roe"] == 30.0
    assert payload["nopat_roic"] == 20.0
    assert payload["ronta"] == 40.0
    assert payload["debt_to_equity"] == 0.0
    assert payload["applied"] == ["cash", "debt", "intangibles"]
    assert payload["input_assumptions"]["debt_to_equity"] == ["debt"]
    assert "do not change Graham criteria or verdicts" in payload["note"]


def test_production_derivation_keeps_strict_row_and_attaches_assumed_quality(monkeypatch):
    strict = {
        "criteria": [{"n": 3, "status": "INSUFFICIENT_DATA"}],
        "verdict": "INDETERMINATE",
    }
    assumed = {
        "criteria": [{"n": 3, "status": "PASS"}],
        "verdict": "PASS",
        "profitability": {"on_equity": 30.0},
        "operating_returns": {
            "nopat_roic": 20.0,
            "ronta": 40.0,
            "operating_return_assumptions": ["intangibles"],
        },
        "debt_to_equity": 0.0,
        "assumptions": ["debt"],
    }
    calls = []

    def fake_derive(*args, assume_absent_zero=False, **kwargs):
        calls.append(assume_absent_zero)
        return "ok", assumed if assume_absent_zero else strict

    monkeypatch.setattr(sync, "_derive", fake_derive)
    bundle = SimpleNamespace(
        cik="0000000001", ticker="TEST", facts={}, dimensioned=None, receipt=None)

    status, payload = sync._derive_evidence(bundle)

    assert status == "ok"
    assert calls == [False, True]
    assert payload["criteria"] == [{"n": 3, "status": "INSUFFICIENT_DATA"}]
    assert payload["verdict"] == "INDETERMINATE"
    assert payload["return_quality_assumption"]["nopat_roic"] == 20.0
    assert payload["return_quality_assumption"]["applied"] == ["debt", "intangibles"]


def test_price_settles_valuation_criteria():
    r = apply_price(row(ttm=5.0, tbvps=10.0), price=40.0)  # P/E 8, P/TBV 4
    c = {x["n"]: x for x in r["criteria"]}
    assert c[1]["status"] == "PASS" and c[1]["value"] == 8.0
    assert c[7]["status"] == "FAIL" and c[7]["value"] == 4.0
    assert r["verdict"] == "FAIL"


def test_us_quote_is_converted_to_the_statement_currency_before_valuation():
    priced = row(ttm=3000.0, tbvps=20000.0)
    priced.update(
        reporting_currency="JPY", quote_currency="USD", shares=10,
        asset_quality={"net_cash": 600000},
        fx={"base": "USD", "counter": "JPY", "rate": 150,
            "asof": "2026-09-01", "source": "test-fx"},
    )

    out = apply_price(priced, price=200.0)
    criteria = {item["n"]: item for item in out["criteria"]}

    assert out["price_reporting_currency"] == 30000.0
    assert criteria[1]["value"] == 10.0
    assert criteria[7]["value"] == 1.5
    assert out["asset_quality"]["market_cap_to_net_cash"] == 0.5


def test_cross_currency_valuation_stays_unknown_without_explicit_fx():
    priced = row(ttm=3000.0, tbvps=20000.0)
    priced.update(reporting_currency="JPY", quote_currency="USD")
    for item in priced["criteria"]:
        if item["n"] in (1, 7):
            item["note"] = "missing: price quote"

    out = apply_price(priced, price=200.0)
    criteria = {item["n"]: item for item in out["criteria"]}

    assert "price_reporting_currency" not in out
    assert criteria[1]["status"] == "INSUFFICIENT_DATA"
    assert criteria[1]["note"] == "missing: USD to JPY exchange rate"
    assert criteria[7]["note"] == "missing: USD to JPY exchange rate"


def test_price_adds_market_cap_to_positive_filing_backed_net_cash_only():
    priced = row()
    priced.update(shares=10, asset_quality={"net_cash": 50})
    out = apply_price(priced, price=20)
    assert out["asset_quality"]["market_cap_to_net_cash"] == 4

    priced = row()
    priced.update(shares=10, asset_quality={"net_cash": -50,
                                            "market_cap_to_net_cash": 99})
    out = apply_price(priced, price=20)
    assert out["asset_quality"].get("market_cap_to_net_cash") is None


def test_live_price_removes_obsolete_missing_price_quote_note():
    r = row(ttm=5.0, tbvps=None)
    r["criteria"][-1]["note"] = "missing: intangibles, price quote"
    out = apply_price(r, price=40.0)
    c7 = {x["n"]: x for x in out["criteria"]}[7]
    assert c7["status"] == "INSUFFICIENT_DATA"
    assert c7["note"] == "missing: intangibles"


def test_every_criterion_passing_gives_pass_verdict():
    r = apply_price(row(ttm=5.0, tbvps=40.0), price=40.0)  # P/E 8, P/TBV 1.0
    assert r["n_pass"] == 6
    assert r["verdict"] == "PASS"


def test_missing_price_leaves_criteria_unknown():
    r = apply_price(row(), price=None)
    assert r["verdict"] == "INDETERMINATE"
    assert all(c["status"] == "INSUFFICIENT_DATA" for c in r["criteria"] if c["n"] in (1, 7))


def test_unlisted_security_refuses_a_quote_for_its_old_symbol():
    r = row(ttm=5.0, tbvps=40.0)
    r.update(
        listed=None, price=40.0, price_asof="2026-08-23T12:00:00+00:00",
        price_session="PRE", market_state="PRE", market_timezone="America/New_York",
        market_state_asof="2026-08-23T12:01:00+00:00", price_source="yahoo",
    )

    out = apply_price(r, price=40.0)

    by_n = {c["n"]: c for c in out["criteria"]}
    assert "price" not in out
    assert "price_asof" not in out
    assert not ({"price_session", "market_state", "market_timezone",
                 "market_state_asof", "price_source"} & set(out))
    assert by_n[1]["status"] == "INSUFFICIENT_DATA" and by_n[1]["value"] is None
    assert by_n[7]["status"] == "INSUFFICIENT_DATA" and by_n[7]["value"] is None
    assert out["verdict"] == "INDETERMINATE"


def test_negative_eps_fails_rather_than_unknown():
    r = apply_price(row(ttm=-2.0), price=40.0)
    assert {c["n"]: c for c in r["criteria"]}[1]["status"] == "FAIL"


def test_not_applicable_never_yields_pass(tmp_path):
    r = apply_price(row(ttm=5.0, tbvps=40.0, others="NOT_APPLICABLE"), price=40.0)
    assert r["verdict"] == "INDETERMINATE"


def test_store_roundtrip_and_staleness(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000000001", "TEST", "Test Co", last_filing="2026-08-01")
    store.put_snapshot(conn, "0000000001", "ok", {"ticker": "TEST", "verdict": "FAIL"})
    assert store.dashboard_rows(conn)[0]["ticker"] == "TEST"
    # a company that filed after our last fetch must be queued for refetch
    assert store.needs_refetch(conn) == ["0000000001"]
    store.upsert_company(conn, "0000000001", "TEST", "Test Co", facts_synced=True)
    assert store.needs_refetch(conn) == []
    # snapshots below the current engine version are recomputed, not refetched
    conn.execute("UPDATE snapshot SET engine_version = 0")
    assert store.needs_recompute(conn) == ["0000000001"]


def test_store_roundtrips_a_directional_fx_history(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    stamp = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    history = PriceHistory(
        quote=Quote(Decimal("150.25"), stamp, "test-fx"),
        closes=((date(2025, 12, 31), Decimal("149.5")),),
    )

    store.set_fx_history(conn, "usd", "jpy", history)
    stored = store.fx_history(conn, "USD", "JPY")

    assert stored["base"] == "USD" and stored["counter"] == "JPY"
    assert stored["rate"] == 150.25
    assert stored["asof"] == stamp.isoformat()
    assert stored["closes"] == [(date(2025, 12, 31), 149.5)]


def test_pending_filing_keeps_recomputed_last_complete_snapshot_and_retries(tmp_path):
    conn = store.connect(tmp_path / "pending.db")
    store.upsert_company(conn, "0000000001", "TEST", "Test company",
                         last_filing="2026-08-27", facts_synced=True)
    pending = {
        "cik": "0000000001", "ticker": "TEST", "criteria": [],
        "data_pending": {
            "kind": "SEC_FACTS_PENDING", "filed": "2026-08-27",
            "accession": "new26", "note": "structured facts pending",
        },
    }

    store.put_snapshot(conn, "0000000001", "pending_facts", pending)
    stored = conn.execute(
        "SELECT status, engine_version, data FROM snapshot WHERE cik = ?",
        ("0000000001",)).fetchone()
    assert stored["status"] == "ok"
    assert stored["engine_version"] == store.ENGINE_VERSION
    assert json.loads(stored["data"])["data_pending"]["accession"] == "new26"
    assert store.needs_refetch(conn) == ["0000000001"]

    settled = {"cik": "0000000001", "ticker": "TEST", "criteria": []}
    store.put_snapshot(conn, "0000000001", "ok", settled)
    assert store.needs_refetch(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM pending_filing").fetchone()[0] == 0


def test_zero_and_non_usd_provider_quotes_are_missing_not_prices():
    now = int(datetime.now(tz=timezone.utc).timestamp())
    assert YahooPriceProvider._quote_from({
        "meta": {"regularMarketPrice": 0, "regularMarketTime": now, "currency": "USD"}
    }) is None
    assert YahooPriceProvider._quote_from({
        "meta": {"regularMarketPrice": 10, "regularMarketTime": now, "currency": "EUR"}
    }) is None
    assert YahooPriceProvider._quote_from({
        "meta": {"regularMarketPrice": 10, "regularMarketTime": now, "currency": "USD"}
    }).price == Decimal("10")
    assert YahooPriceProvider._quote_from({
        "meta": {"regularMarketPrice": 210, "regularMarketTime": now, "currency": "EUR"}
    }, expected_currency="EUR").price == Decimal("210")


def test_provider_prefers_newer_premarket_bar_and_discloses_market_state():
    pre_start = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    pre_end = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    regular_end = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    post_end = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    regular_quote = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    premarket_bar = datetime(2026, 8, 28, 13, 5, tzinfo=timezone.utc)
    result = {
        "meta": {
            "regularMarketPrice": 61.47,
            "regularMarketTime": int(regular_quote.timestamp()),
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "currentTradingPeriod": {
                "pre": {"start": int(pre_start.timestamp()), "end": int(pre_end.timestamp())},
                "regular": {"start": int(pre_end.timestamp()), "end": int(regular_end.timestamp())},
                "post": {"start": int(regular_end.timestamp()), "end": int(post_end.timestamp())},
            },
        },
        "timestamp": [int(premarket_bar.timestamp())],
        "indicators": {"quote": [{"close": [52.75]}]},
    }

    quote = YahooPriceProvider._quote_from(
        result, now=datetime(2026, 8, 28, 13, 10, tzinfo=timezone.utc)
    )

    assert quote.price == Decimal("52.75")
    assert quote.asof == premarket_bar
    assert quote.session == "PRE"
    assert quote.market_state == "PRE"
    assert quote.market_timezone == "America/New_York"
    assert quote.market_state_asof == datetime(2026, 8, 28, 13, 10, tzinfo=timezone.utc)


def test_closed_market_keeps_last_after_hours_price_but_says_closed():
    start = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    regular_end = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    post_end = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    result = {
        "meta": {
            "regularMarketPrice": 50,
            "regularMarketTime": int(regular_end.timestamp()) - 1,
            "currency": "USD",
            "currentTradingPeriod": {
                "pre": {"start": int(start.timestamp()) - 60, "end": int(start.timestamp())},
                "regular": {"start": int(start.timestamp()), "end": int(regular_end.timestamp())},
                "post": {"start": int(regular_end.timestamp()), "end": int(post_end.timestamp())},
            },
        },
        "timestamp": [int(post_end.timestamp()) - 60],
        "indicators": {"quote": [{"close": [51.25]}]},
    }

    quote = YahooPriceProvider._quote_from(
        result, now=datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
    )

    assert quote.price == Decimal("51.25")
    assert quote.session == "POST"
    assert quote.market_state == "CLOSED"


def test_no_extended_trade_keeps_regular_quote_while_premarket_is_open():
    pre_start = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    regular_start = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    regular_end = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    regular_quote = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    result = {
        "meta": {
            "regularMarketPrice": 10,
            "regularMarketTime": int(regular_quote.timestamp()),
            "currency": "USD",
            "currentTradingPeriod": {
                "pre": {"start": int(pre_start.timestamp()), "end": int(regular_start.timestamp())},
                "regular": {"start": int(regular_start.timestamp()), "end": int(regular_end.timestamp())},
            },
        },
        "timestamp": [],
        "indicators": {"quote": [{"close": []}]},
    }

    quote = YahooPriceProvider._quote_from(
        result, now=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    )

    assert quote.price == Decimal("10")
    assert quote.session == "REGULAR"
    assert quote.market_state == "PRE"


def test_provider_carries_declared_split_events_with_the_price_history(monkeypatch):
    stamp = int(datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp())
    provider = YahooPriceProvider()
    monkeypatch.setattr(provider, "_chart", lambda *_, **__: {
        "meta": {"regularMarketPrice": 10, "regularMarketTime": stamp, "currency": "USD"},
        "timestamp": [stamp],
        "indicators": {"quote": [{"close": [10]}]},
        "events": {"splits": {str(stamp): {
            "date": stamp, "numerator": 1, "denominator": 10,
        }}},
    })

    history = provider.history("TEST")
    provider._http.close()

    assert history.splits == ((date(2026, 8, 22), Decimal("10")),)


def test_provider_reads_usd_to_reporting_currency_in_a_fixed_direction(monkeypatch):
    stamp = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    calls = []
    provider = YahooPriceProvider()

    def fake_chart(symbol, range_, interval, include_pre_post=False):
        calls.append((symbol, range_, interval, include_pre_post))
        return {
            "meta": {"regularMarketPrice": 150, "regularMarketTime": stamp,
                     "currency": "JPY"},
            "timestamp": [stamp],
            "indicators": {"quote": [{"close": [149.5]}]},
        }

    monkeypatch.setattr(provider, "_chart", fake_chart)
    history = provider.exchange_rate_history("USD", "JPY")
    provider.close()

    assert calls == [("USDJPY=X", "10y", "1wk", False)]
    assert history.quote.price == Decimal("150")
    assert history.quote.source == "yahoo-fx:USDJPY=X"
    assert history.closes == ((date(2026, 9, 1), Decimal("149.5")),)


def test_historical_multiples_use_the_exchange_rate_at_each_fiscal_end():
    priced = {
        "reporting_currency": "JPY", "quote_currency": "USD",
        "ttm_eps_vintage": {"2025-12-31": 300.0},
        "annual_ratios": {2025: {
            "end": "2025-12-31", "bvps": 2000.0,
            "tbvps": 1800.0, "ncavps": 1000.0,
        }},
    }

    sync._price_the_ratio_history(
        priced,
        [(date(2025, 12, 20), 20.0)],
        [(date(2025, 12, 19), 150.0)],
    )

    cell = priced["annual_ratios"][2025]
    assert cell["quote_price"] == 20.0
    assert cell["fx_rate"] == 150.0
    assert cell["price"] == 3000.0
    assert cell["pe"] == 10.0
    assert cell["pb"] == 1.5


def test_history_requests_extended_bars_only_for_intraday_quote(monkeypatch):
    stamp = int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    calls = []
    provider = YahooPriceProvider()

    def fake_chart(ticker, range_, interval, include_pre_post=False):
        calls.append((range_, interval, include_pre_post))
        return {
            "meta": {"regularMarketPrice": 10, "regularMarketTime": stamp, "currency": "USD"},
            "timestamp": [stamp],
            "indicators": {"quote": [{"close": [10]}]},
        }

    monkeypatch.setattr(provider, "_chart", fake_chart)
    history = provider.history("TEST")
    provider._http.close()

    assert history is not None
    assert calls == [("5y", "1wk", False), ("1d", "5m", True)]


def test_quote_only_export_updates_all_rows_and_retains_a_dated_quote_on_failure(
        tmp_path, monkeypatch):
    dashboard = tmp_path / "dashboard.json"
    dashboard.write_text(json.dumps({"rows": [
        {"cik": "1", "price": 1, "price_asof": "2026-08-27T20:00:00+00:00",
         "index_memberships": ["S&P 500"]},
        {"cik": "2", "price": 2, "price_asof": "2026-08-27T20:00:00+00:00",
         "index_memberships": ["Nasdaq Comp"]},
    ]}), encoding="utf-8")
    rows = [
        {"cik": "1", "ticker": "ONE", "listed": "y"},
        {"cik": "2", "ticker": "TWO", "listed": "y"},
    ]
    states = {}

    class Connection:
        def commit(self):
            pass

    class Provider:
        closed = False

        def quote(self, ticker):
            if ticker == "TWO":
                return None
            stamp = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
            return Quote(price=Decimal("10"), asof=stamp, source="test",
                         session="REGULAR", market_state="CLOSED",
                         market_timezone="America/New_York", market_state_asof=stamp)

        def history(self, ticker):
            raise AssertionError("hourly refresh must not fetch five-year history")

        def close(self):
            self.closed = True

    provider = Provider()
    monkeypatch.setattr(sync, "DASHBOARD_JSON", dashboard)
    monkeypatch.setattr(sync, "YahooPriceProvider", lambda: provider)
    monkeypatch.setattr(sync.store, "needs_recompute", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sync.store, "dashboard_rows", lambda _conn: rows)
    monkeypatch.setattr(sync.store, "price_history", lambda *_args: [])
    monkeypatch.setattr(sync.store, "set_state", lambda _conn, key, value: states.__setitem__(key, value))
    monkeypatch.setattr(sync, "apply_price", lambda row_, _price: row_)
    monkeypatch.setattr(sync, "_price_stats_row", lambda _row, _closes: None)
    monkeypatch.setattr(sync, "_mark_peer_efficiency", lambda _rows: None)
    monkeypatch.setattr(sync.profiles, "enrich", lambda _row: {})
    monkeypatch.setattr(sync, "_price_the_ratio_history", lambda _row, _closes, _fx=(): None)

    sync.quotes(Connection(), progress=lambda *_args: None)

    payload = json.loads(dashboard.read_text(encoding="utf-8"))
    by_cik = {row_["cik"]: row_ for row_ in payload["rows"]}
    assert by_cik["1"]["price"] == 10
    assert by_cik["1"]["index_memberships"] == ["S&P 500"]
    assert "quote_refresh_warning" not in by_cik["1"]
    assert by_cik["2"]["price"] == 2
    assert by_cik["2"]["price_asof"] == "2026-08-27T20:00:00+00:00"
    assert by_cik["2"]["index_memberships"] == ["Nasdaq Comp"]
    assert by_cik["2"]["quote_refresh_warning"]["kind"] == "QUOTE_REFRESH_FAILED"
    assert states["last_quote_refresh_updated"] == "1"
    assert states["last_quote_refresh_failed"] == "1"
    assert provider.closed is True
    assert not dashboard.with_suffix(".json.tmp").exists()


def test_history_rejects_unexplained_rescaling_and_truncation():
    fetched = datetime(2026, 8, 20, tzinfo=timezone.utc)
    old = tuple((date(2026, 1, 1) + timedelta(days=7 * i), Decimal("10"))
                for i in range(30))
    rescaled = tuple((day, value * 10) for day, value in old)
    accepted, warning = _validated_price_history(fetched, old, rescaled)
    assert accepted is None and "without matching split" in warning

    accepted, warning = _validated_price_history(fetched, old, old[-5:])
    assert accepted is None and ("oldest coverage" in warning or "observations" in warning)


def test_history_accepts_a_corroborated_reverse_split_restatement():
    fetched = datetime(2026, 8, 20, tzinfo=timezone.utc)
    split_day = date(2026, 8, 22)
    old = tuple((date(2026, 7, 1) + timedelta(days=7 * i), Decimal("2"))
                for i in range(8))
    new = tuple((day, value * (10 if day < split_day else 1)) for day, value in old)

    accepted, warning = _validated_price_history(
        fetched, old, new, ((split_day, Decimal("10")),), (Decimal("10"),))

    assert warning is None
    assert accepted == new


def test_history_keeps_contemporaneous_prices_until_filings_match_the_split():
    fetched = datetime(2026, 8, 20, tzinfo=timezone.utc)
    split_day = date(2026, 8, 22)
    old = tuple((date(2026, 7, 1) + timedelta(days=7 * i), Decimal("2"))
                for i in range(8))
    adjusted = tuple((day, value * (10 if day < split_day else 1))
                     for day, value in old)

    accepted, warning = _validated_price_history(
        fetched, old, adjusted, ((split_day, Decimal("10")),))

    assert accepted == old
    assert "per-share history does not yet reflect" in warning

    # A prior buggy refresh may already have stored the adjusted series. The same
    # declared event still lets the validator repair it without a ticker override.
    repaired, warning = _validated_price_history(
        datetime(2026, 8, 23, tzinfo=timezone.utc), adjusted, adjusted,
        ((split_day, Decimal("10")),))
    assert repaired == old
    assert "per-share history does not yet reflect" in warning


def test_per_share_changes_supply_the_independent_split_basis():
    previous = {
        "annual_eps": {"2025": -2},
        "annual_ratios": {"2025": {"bvps": 4, "tbvps": 3}},
    }
    current = {
        "annual_eps": {"2025": -20},
        "annual_ratios": {"2025": {"bvps": 40, "tbvps": 30}},
    }
    factors = sync._per_share_restatement_factors(previous, current)
    assert factors == (Decimal("10"), Decimal("10"), Decimal("10"))


def test_price_stats_are_withheld_while_price_and_filing_split_bases_differ():
    row_ = {
        "price": 20,
        "price_history_warning": {"note": (
            "the provider declared a split that the filing-derived per-share history "
            "does not yet reflect; contemporaneous historical closes remain in use")},
    }
    closes = ((date(2026, 8, 1), Decimal("10")),
              (date(2026, 8, 8), Decimal("12")))
    assert sync._price_stats_row(row_, closes) is None


def test_routine_recompute_defers_ineligible_cache_rows_until_they_can_surface(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    companies = (
        ("0000000001", "ACTIVE"),
        ("0000000002", None),
        ("0000000003", "PREF-PA"),
    )
    for cik, ticker in companies:
        store.upsert_company(conn, cik, ticker, f"Company {cik}")
        store.put_snapshot(conn, cik, "ok", {"cik": cik})
    conn.execute("UPDATE snapshot SET engine_version = 0")

    assert store.needs_recompute(conn) == [cik for cik, _ in companies]
    assert store.needs_recompute(conn, eligible_only=True) == ["0000000001"]

    # A later SEC mapping makes the deferred filer actionable before export.
    store.upsert_company(conn, "0000000002", "NEW", "Newly listed")
    assert store.needs_recompute(conn, eligible_only=True) == [
        "0000000001", "0000000002"]
    assert store.stats(conn)["stale"] == 2
    assert store.stats(conn)["deferred_stale"] == 1


def test_known_ticker_without_filings_is_not_queued(tmp_path):
    # the ticker map upserts thousands of companies with no filing date; if that
    # wrote '' instead of NULL, every one of them would queue for refetch
    conn = store.connect(tmp_path / "t.db")
    for i in range(3):
        store.upsert_company(conn, f"000000000{i}", f"T{i}", f"Co {i}")  # no last_filing
        store.upsert_company(conn, f"000000000{i}", f"T{i}", f"Co {i}")  # seen again
    assert conn.execute(
        "SELECT COUNT(*) FROM company WHERE last_filing IS NOT NULL").fetchone()[0] == 0
    assert store.needs_refetch(conn) == []
    # only a company that actually filed is queued
    store.upsert_company(conn, "0000000001", "T1", "Co 1", last_filing="2026-08-12")
    assert store.needs_refetch(conn) == ["0000000001"]


def test_upsert_keeps_newest_filing_date(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000000002", "A", "A Co", last_filing="2026-08-05")
    store.upsert_company(conn, "0000000002", None, None, last_filing="2026-07-01")
    row_ = conn.execute("SELECT last_filing, ticker FROM company").fetchone()
    assert row_["last_filing"] == "2026-08-05"  # older filing must not overwrite
    assert row_["ticker"] == "A"                # nor blank the ticker


def test_dormant_filer_gets_no_pe_or_ptbv():
    """A live price against decade-old figures is a confident, meaningless number."""
    r = row(ttm=2.42, tbvps=30.0)
    r.update(earnings_asof="2010-12-31", balance_sheet_date="2010-12-31",
             price_asof="2026-08-14T20:00:00+00:00")
    out = apply_price(r, price=40.0)
    c = {x["n"]: x for x in out["criteria"]}
    assert c[1]["status"] == "INSUFFICIENT_DATA" and c[1]["value"] is None
    assert c[7]["status"] == "INSUFFICIENT_DATA" and c[7]["value"] is None
    assert "stopped filing" in c[1]["note"]
    assert out["verdict"] == "INDETERMINATE"


def test_recent_filer_still_priced_normally():
    r = row(ttm=5.0, tbvps=40.0)
    r.update(earnings_asof="2026-06-30", balance_sheet_date="2026-06-30",
             price_asof="2026-08-14T20:00:00+00:00")
    out = apply_price(r, price=40.0)
    c = {x["n"]: x for x in out["criteria"]}
    assert c[1]["status"] == "PASS" and c[1]["value"] == 8.0
    assert c[7]["status"] == "PASS"


def test_annual_only_filer_within_the_lag_is_not_penalised():
    # a 10-K-only filer legitimately lags around 15 months before the next one lands
    r = row(ttm=5.0, tbvps=40.0)
    r.update(earnings_asof="2025-06-30", balance_sheet_date="2025-06-30",
             price_asof="2026-08-14T20:00:00+00:00")
    out = apply_price(r, price=40.0)
    assert {x["n"]: x for x in out["criteria"]}[1]["status"] == "PASS"


def test_missing_table_is_created_on_an_existing_database(tmp_path):
    """A release that adds a table must not require deleting the database."""
    db = tmp_path / "t.db"
    conn = store.connect(db)
    conn.execute("DROP TABLE tracked")   # a database made before the feature existed
    conn.commit()
    conn.close()
    conn = store.connect(db)             # reopening must restore it
    assert store.tracked(conn) == []


def test_track_and_untrack(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000000001", "GHC", "Graham Holdings Co")
    store.track(conn, "0000000001")
    assert [r["ticker"] for r in store.tracked(conn)] == ["GHC"]
    store.track(conn, "0000000001", note="watch the education segment")
    assert store.tracked(conn)[0]["note"] == "watch the education segment"
    assert len(store.tracked(conn)) == 1                 # tracking twice is not a duplicate
    assert store.untrack(conn, "0000000001") is True
    assert store.tracked(conn) == []
    assert store.untrack(conn, "0000000001") is False    # already gone


def test_index_membership_parsers():
    from screener.sources.indexes import djia_ciks, nasdaq100, sp500
    row = ('<tr><td><a href="/wiki/Comp_{i}">C{i}</a></td>'
           '<td>000000{i:04d}\n</td></tr>')
    sp_html = ('<table id="constituents">'
               + "".join(row.format(i=i) for i in range(1, 502)) + "</table>")
    sp = sp500(sp_html)
    assert len(sp) == 500 and sp["0000000002"] == "Comp_2"  # first row is the header
    # under ~480 rows means the page layout broke — refuse, never half an index
    assert sp500('<table id="constituents"><tr><td>x</td></tr></table>') is None

    cells = "".join(f'<td><a href="/wiki/Comp_{i}">Company {i}</a> ↑ </td>' for i in range(2, 32))
    dj_html = f'<table class="wikitable"><tr>{cells}</tr></table>'
    dj = djia_ciks(sp, dj_html)
    assert len(dj) == 30 and "0000000030" in dj
    # a footnote cell with extra prose must not add a 31st member
    noisy = dj_html.replace("</table>",
        '<tr><td>spun off <a href="/wiki/Comp_99">Company 99</a> in 2023</td></tr></table>')
    assert djia_ciks(sp, noisy) == dj

    def tick(i):
        return f"T{chr(65 + i // 26)}{chr(65 + i % 26)}"
    n_html = ('<table id="constituents">'
              + "".join(f'<tr><td>{tick(i)}</td><td><a href="/wiki/X">X</a></td></tr>'
                        for i in range(102)) + "</table>")
    assert len(nasdaq100(n_html)) == 101  # first row is the header


def test_row_carries_per_figure_provenance_and_series_mix():
    """Release 3b: every extracted figure names its tag and filing; a series
    stitched from two tags discloses which years came from which."""
    from tests.helpers import GAAP, facts_doc, tagdata, dur
    from screener.sync import _derive

    gaap = dict(GAAP)
    # EPS 2021 under a different element -> a scope switch worth disclosing
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        e for e in GAAP["EarningsPerShareDiluted"]["units"]["USD/shares"]
        if not e["start"].startswith("2021")])
    gaap["EarningsPerShareBasicAndDiluted"] = tagdata("USD/shares", [
        dur("2021-01-01", "2021-12-31", 3.0, accn="k21", filed="2022-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2024-01-01", "2024-12-31", 9.8e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 9.6e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["CommonStockDividendsPerShareDeclared"] = tagdata("USD/shares", [
        dur("2025-07-01", "2025-09-30", 0.20, form="10-Q", accn="q325"),
        dur("2025-10-01", "2025-12-31", 0.20, accn="k25"),
        dur("2026-01-01", "2026-03-31", 0.20, form="10-Q", accn="q126"),
    ])
    status, row = _derive("0000000001", "TEST", facts_doc(gaap))
    assert status == "ok"
    src = row["sources"]["total_assets"]
    assert src["tag"] == "us-gaap:Assets"
    assert src["form"] and src["accn"] and src["end"]
    assert row["sources"]["goodwill"]["tag"] == "us-gaap:Goodwill"
    assert row["annual_weighted_shares"]["2025"] == 9.6e9
    assert row["sources"]["weighted_shares"]["tag"] == (
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")
    recurring = row["sources"]["recurring_dividend_per_share"]
    assert recurring["start"] == "2026-01-01"
    assert recurring["end"] == "2026-03-31"
    assert recurring["unit"] == "USD/shares"
    mix = row["series_mix"]["eps"]
    assert mix["us-gaap:EarningsPerShareBasicAndDiluted"] == [2021]
    assert 2025 in mix["us-gaap:EarningsPerShareDiluted"]


def test_asset_quality_and_lease_metrics_are_payload_evidence_not_assumptions():
    from tests.helpers import OE_GAAP, dur, facts_doc, inst, tagdata
    from screener.sync import _derive

    gaap = dict(OE_GAAP)
    gaap.update({
        "Liabilities": tagdata("USD", [inst("2026-03-31", 100e9, accn="q126")]),
        "InventoryNet": tagdata("USD", [inst("2026-03-31", 50e9, accn="q126")]),
        "AccountsReceivableNetCurrent": tagdata(
            "USD", [inst("2026-03-31", 60e9, accn="q126")]),
        "ShortTermInvestments": tagdata(
            "USD", [inst("2026-03-31", 10e9, accn="q126")]),
        "LongTermDebtNoncurrent": tagdata(
            "USD", [inst("2026-03-31", 20e9, accn="q126")]),
        "OperatingLeaseLiability": tagdata(
            "USD", [inst("2026-03-31", 30e9, accn="q126")]),
        "OperatingLeaseCost": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 8e9, accn="k25", filed="2026-02-15")]),
        "InterestExpense": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 2e9, accn="k25", filed="2026-02-15")]),
    })
    status, row = _derive("0000000001", "TEST", facts_doc(gaap))
    assert status == "ok"
    quality = row["asset_quality"]
    assert quality["inventory"] == 50e9 and quality["inventory_to_ncav"] == 25
    assert quality["receivables"] == 60e9 and quality["receivables_to_ncav"] == 30
    assert quality["net_cash"] == 30e9
    assert row["operating_lease_liability"] == 30e9
    assert row["lease_adjusted_debt"] == 50e9
    assert row["fixed_charge_coverage"] == pytest.approx(10.8)
    assert row["sources"]["inventory"]["accn"] == "q126"
    assert row["sources"]["fixed_charge_coverage"]["components"]


def test_peer_efficiency_needs_a_real_peer_group():
    from screener.sync import _mark_peer_efficiency
    def company(ticker, industry, income, revenue):
        return {"ticker": ticker, "industry": industry,
                "annual_operating_income": {"2025": income}, "annual_revenue": {"2025": revenue}}
    # a group of six: five healthy, one far behind
    rows = [company(f"P{i}", "Railroads", 30, 100) for i in range(5)]
    rows.append(company("LAGGARD", "Railroads", 10, 100))
    # a company alone in its industry has no median to be measured against
    rows.append(company("ALONE", "Zeppelins", 1, 100))
    _mark_peer_efficiency(rows)
    laggard = next(r for r in rows if r["ticker"] == "LAGGARD")
    assert laggard["peer_efficiency"]["behind"] is True
    assert laggard["peer_efficiency"]["industry_median"] == 30.0
    assert next(r for r in rows if r["ticker"] == "P0")["peer_efficiency"]["behind"] is False
    assert "peer_efficiency" not in next(r for r in rows if r["ticker"] == "ALONE")
    assert all("_margin" not in r for r in rows)


# --- what a filing index proves happened ---

def submissions(rows, extra=()):
    """A submissions index shaped like SEC's: parallel arrays, one per filing."""
    forms, dates, items, accns = zip(*rows) if rows else ((), (), (), ())
    return {"filings": {"recent": {
        "form": list(forms) + [f for f, *_ in extra],
        "filingDate": list(dates) + [d for _, d, *_ in extra],
        "items": list(items) + ["" for _ in extra],
        "accessionNumber": list(accns) + [a for *_, a in extra],
    }}}


def test_material_items_are_read_and_the_rest_ignored():
    seen, scanned_from = material_events(submissions([
        ("8-K", "2026-03-02", "4.02,9.01", "a-1"),   # non-reliance, with an exhibit
        ("8-K", "2025-11-10", "2.02,9.01", "a-2"),   # results of operations: routine
        ("8-K", "2024-06-01", "5.02", "a-3"),        # officer change: cannot be read
        ("10-K", "2026-02-15", "", "a-4"),
    ]))
    assert seen == [{"filed": "2026-03-02", "item": "4.02", "accn": "a-1"}]
    assert scanned_from == "2024-06-01"


def test_the_window_is_what_the_index_could_show():
    """Wells Fargo's thousand most recent filings reach back fourteen months. A
    scan may only claim the window its data covers."""
    _, scanned_from = material_events(submissions([("8-K", "2025-06-05", "3.01", "a-1")]))
    assert scanned_from == "2025-06-05"


def test_an_index_with_no_filings_claims_no_window():
    assert material_events({"filings": {"recent": {}}}) == ([], None)


def test_events_round_trip_and_replace_wholesale(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000000001", "TEST", "Test Co")
    store.put_snapshot(conn, "0000000001", "ok", {"cik": "0000000001", "ticker": "TEST"})
    store.set_events(conn, "0000000001", [
        {"filed": "2026-03-02", "item": "4.02", "accn": "a-1"},
        {"filed": "2025-01-05", "item": "3.01", "accn": "a-2"},
    ], "2021-01-01")
    row_ = store.dashboard_rows(conn)[0]
    assert [e["item"] for e in row_["filing_events"]] == ["3.01", "4.02"]  # oldest first
    assert row_["events_from"] == "2021-01-01"
    # a rescan is the whole truth about its window: an amended index drops rows
    store.set_events(conn, "0000000001", [
        {"filed": "2026-03-02", "item": "4.02", "accn": "a-1"}], "2022-01-01")
    row_ = store.dashboard_rows(conn)[0]
    assert [e["item"] for e in row_["filing_events"]] == ["4.02"]
    assert row_["events_from"] == "2022-01-01"


def test_a_company_never_scanned_is_distinguishable_from_one_with_no_events(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000000002", "NEW", "New Co")
    store.put_snapshot(conn, "0000000002", "ok", {"cik": "0000000002", "ticker": "NEW"})
    row_ = store.dashboard_rows(conn)[0]
    assert row_["filing_events"] == [] and row_["events_from"] is None


def test_the_price_pass_does_not_resurrect_a_criterion_the_engine_refused():
    """SM Energy's earnings are struck on 115.0M weighted shares while its current
    count is 237.5M. The engine withholds criterion 1 for it, and apply_price used to
    recompute the ratio from ttm_eps anyway and ship PASS."""
    r = row(ttm=5.64, tbvps=10.0)
    r["basis_conflict"] = "FY2025 earnings are struck on 115.0M weighted shares..."
    out = apply_price(r, price=30.0)
    c = {x["n"]: x for x in out["criteria"]}
    assert c[1]["status"] == "INSUFFICIENT_DATA" and c[1]["value"] is None
    assert c[7]["status"] == "INSUFFICIENT_DATA"


def test_a_yield_above_par_is_not_published():
    """29 rows shipped a yield over 100%, topping at 2,240,506%. The engine refuses
    them; the export pass was publishing them regardless."""
    r = row(ttm=5.0, tbvps=10.0)
    r["recurring_dividend_per_share"] = 1770000.0
    out = apply_price(r, price=79.0)
    c5 = {x["n"]: x for x in out["criteria"]}[5]
    assert c5["value"] is None and "not meaningful" in c5["note"]


def test_price_uses_recurring_dividend_and_discloses_special_inclusive_cash():
    r = row(ttm=5.0, tbvps=10.0)
    r["dividend_per_share"] = 4.40
    r["recurring_dividend_per_share"] = 1.40
    r["sources"] = {"recurring_dividend_per_share": {"end": "2026-05-02"}}

    out = apply_price(r, price=43.48)

    c5 = {x["n"]: x for x in out["criteria"]}[5]
    assert c5["value"] == 3.22
    assert "special dividends excluded" in c5["note"]
    assert "trailing cash was $4.40" in c5["note"]


def test_profitability_includes_finkles_return_on_net_tangible_assets():
    """Book and tangible returns share the fiscal-year income and recency guard.

    Missing intangible evidence withholds Finkle's ratio, and a balance sheet from
    a different era withholds both capital-return measures.
    """
    from decimal import Decimal
    from datetime import date
    from screener.sync import _debt_to_equity, _profitability

    class Snap:
        annual_revenue = {2025: type("F", (), {"value": Decimal("100000000"),
                                               "provenance": None})()}
        annual_net_income = {2025: type("F", (), {
            "value": Decimal("12000000"),
            "provenance": type("P", (), {"period_end": date(2025, 12, 31)})()})()}
        annual_operating_income = {2025: type("F", (), {"value": Decimal("18000000"),
                                                        "provenance": None})()}
        debt_provenance = type("P", (), {"period_end": date(2026, 3, 31)})()
        total_assets = type("F", (), {"value": Decimal("200000000")})()
        total_liabilities = type("F", (), {"value": Decimal("140000000")})()
        long_term_debt = type("F", (), {"value": Decimal("30000000"),
                                          "provenance": debt_provenance})()
        short_term_debt = type("F", (), {"value": Decimal("5000000"),
                                           "provenance": debt_provenance})()
        # The rollup is smaller than the complete long + short representation;
        # debt/equity must use the same conservative reconciliation as criterion 3.
        total_debt = type("F", (), {"value": Decimal("32000000"),
                                      "provenance": debt_provenance})()
        assumed_zero = frozenset()
        goodwill = type("F", (), {"value": Decimal("10000000")})()
        intangibles = type("F", (), {"value": Decimal("5000000")})()
        preferred_stock = noncontrolling_interest = temporary_equity = None
        balance_sheet_date = date(2026, 3, 31)

    history = {2025: {"gross_margin": 42.5,
                      "average_common_equity": 50_000_000,
                      "return_on_equity": 24.0}}
    p = _profitability(Snap(), history)
    assert p["net"] == 12.0 and p["operating"] == 18.0
    assert p["gross"] == 42.5
    assert p["on_book"] == 20.0          # 12M on 60M of common equity
    assert p["on_equity"] == 24.0        # 12M on 50M average common equity
    assert p["on_net_tangible_assets"] == 26.67  # 12M on 45M after intangibles
    assert p["net_tangible_assets"] == 45_000_000
    assert _debt_to_equity(Snap()) == round(35 / 60, 4)

    Snap.intangibles = None
    assert _profitability(Snap())["on_net_tangible_assets"] is None
    Snap.intangibles = type("F", (), {"value": Decimal("5000000")})()

    Snap.balance_sheet_date = date(2030, 3, 31)   # five years past the earnings
    assert _profitability(Snap())["on_book"] is None
    assert _profitability(Snap())["on_net_tangible_assets"] is None


def test_a_symbol_belongs_to_the_company_sec_names_today(tmp_path):
    """ATAI rendered twice — $1.77B and $2.72B at the same price — because the
    predecessor kept the ticker after the successor took it. Atai Beckley N.V. filed
    a Form 15; AtaiBeckley Inc. files the 10-Qs. upsert_company can only add a
    ticker, never clear one, so the conflict had to be resolved explicitly."""
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0001840904", "ATAI", "Atai Beckley N.V.")
    store.upsert_company(conn, "0002081043", "ATAI", "AtaiBeckley Inc.")
    for cik in ("0001840904", "0002081043"):
        store.put_snapshot(conn, cik, "ok", {"cik": cik, "ticker": "ATAI"})
    assert len(store.dashboard_rows(conn)) == 2          # the defect

    store.resolve_ticker_conflicts(conn, {"0002081043": ("ATAI", "AtaiBeckley Inc.")})
    rows = store.dashboard_rows(conn)
    assert [r["cik"] for r in rows] == ["0002081043"]
    # the predecessor keeps everything except the claim to the symbol
    assert conn.execute("SELECT COUNT(*) FROM snapshot WHERE cik = '0001840904'").fetchone()[0] == 1


def test_new_ticker_index_prefers_the_cover_verified_common_share_over_a_spac_unit(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.set_cover(conn, "0002015502", [
        {"symbol": "LPAA", "title": "Class A Ordinary Shares", "ratio": None},
        {"symbol": "LPAAU", "title": (
            "Units, each consisting of one Class A Ordinary Share and one-half "
            "of one redeemable Warrant"), "ratio": None},
    ], "cover25")

    class Edgar:
        def _cached(self, *_args):
            return {
                "0": {"cik_str": 2015502, "ticker": "LPAAU", "title": "Launch One"},
                "1": {"cik_str": 2015502, "ticker": "LPAA", "title": "Launch One"},
                "2": {"cik_str": 2015502, "ticker": "LPAAW", "title": "Launch One"},
            }

    selected = sync._index_tickers(conn, Edgar())

    assert selected["0002015502"] == ("LPAA", "Launch One")
    assert conn.execute(
        "SELECT ticker FROM company WHERE cik = '0002015502'").fetchone()[0] == "LPAA"


def test_ticker_index_preserves_an_established_live_symbol_when_cover_is_partial(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_company(conn, "0000005513", "UNM", "Unum")
    store.set_cover(conn, "0000005513", [
        {"symbol": "UNMA", "title": "6.250% Junior Subordinated Notes", "ratio": None},
    ], "cover26")

    class Edgar:
        def _cached(self, *_args):
            return {
                "0": {"cik_str": 5513, "ticker": "UNMA", "title": "Unum"},
                "1": {"cik_str": 5513, "ticker": "UNM", "title": "Unum"},
            }

    selected = sync._index_tickers(conn, Edgar())

    assert selected["0000005513"] == ("UNM", "Unum")


def test_the_cover_parser_reads_the_ratio_the_data_cannot_carry():
    """dei:Security12bTitle is text under a share-class axis, so Company Facts strips it
    twice over. Filers word it three ways and label it two ways; all of them are one
    sentence naming the security the ticker prices."""
    from screener.sources import cover

    assert cover.depositary_ratio(
        "American Depositary Shares, each representing 13 Ordinary Shares") == 13
    assert cover.depositary_ratio(
        "American Depositary Shares, each representing three ordinary shares") == 3
    assert cover.depositary_ratio(
        "American Depository Shares, each representing 2,000 Ordinary Shares") == 2000
    assert cover.depositary_ratio(
        "American depositary shares (one American depositary share representing "
        "twenty Class A Ordinary Shares, par value $0.003 per share)") == 20
    assert cover.depositary_ratio(
        "Series B common shares, in the form of American Depositary Shares each "
        "representing one Series B share") == 1
    assert cover.depositary_ratio(
        "American Depositary Shares (as evidenced by American Depositary Receipts), "
        "each representing 2,000 shares of Common Stock") == 2000
    assert cover.depositary_ratio(
        "American Depositary Shares each representing 1 share") == 1
    assert cover.depositary_ratio(
        "Each American Depositary Share representing ten shares") == 10
    # an ordinary class says nothing about a ratio, which is the answer for most filers
    assert cover.depositary_ratio("Common Stock, $0.25 Par Value") is None
    assert cover.depositary_ratio("1.875% Notes Due 2026") is None
    assert cover.is_depositary_security("American Depositary Shares") is True
    assert cover.is_depositary_security("American Depository Receipts") is True
    assert cover.is_depositary_security("Ordinary Shares") is False
    assert cover.is_common_equity_security("Class A shares, no par value") is True
    assert cover.is_common_equity_security("Common Units representing limited partner interests") is True
    assert cover.is_common_equity_security("Credit Suisse Gold Shares ETNs due 2033") is False
    assert cover.is_common_equity_security("Preferred shares, par value $0.01") is False

    page = ("Title of each class American Depositary Shares, each representing 13 Ordinary "
            "Shares, par value $0.0001 per share Trading Symbol(s) ONC Name of each exchange")
    assert cover.securities(page) == [
        {"title": "American Depositary Shares, each representing 13 Ordinary Shares, "
                  "par value $0.0001 per share", "symbol": "ONC"}]

    toyota = (
        "American Depositary Shares [Member] Trading Symbol TM "
        "Title of 12(b) Security American Depositary Shares "
        "Security Exchange Name NYSE"
    )
    assert cover.securities(toyota) == [
        {"title": "American Depositary Shares", "symbol": "TM"}]

    # OACC's rendered report orders each class as symbol, exchange, then title.
    # A whole-document regex crossed the member boundaries and attached the
    # warrant's symbol to the ordinary-share title.
    oacc = """
      <table>
        <tr><td>Trading Symbol</td><td>OACCU</td></tr>
        <tr><td>Security Exchange Name</td><td>NASDAQ</td></tr>
        <tr><td>Title of 12(b) Security</td><td>Units, each consisting of one
          Class A ordinary share and one-fifth of one redeemable warrant</td></tr>
        <tr><td>Entity Common Stock, Shares Outstanding</td><td>19,783,010</td></tr>
        <tr><td>Trading Symbol</td><td>OACC</td></tr>
        <tr><td>Security Exchange Name</td><td>NASDAQ</td></tr>
        <tr><td>Title of 12(b) Security</td><td>Class A ordinary shares included
          as part of the units</td></tr>
        <tr><td>Trading Symbol</td><td>OACCW</td></tr>
        <tr><td>Security Exchange Name</td><td>NASDAQ</td></tr>
        <tr><td>Title of 12(b) Security</td><td>Redeemable warrants included as
          part of the units</td></tr>
      </table>
    """
    assert cover.securities(oacc) == [
        {"title": "Units, each consisting of one Class A ordinary share and "
                  "one-fifth of one redeemable warrant", "symbol": "OACCU"},
        {"title": "Class A ordinary shares included as part of the units",
         "symbol": "OACC"},
        {"title": "Redeemable warrants included as part of the units",
         "symbol": "OACCW"},
    ]

    # NYSE renders preferred tickers with spaces. Truncating "GLP pr B" to GLP
    # made this second class overwrite the common-unit cover record.
    multi = (
        "Title of 12(b) Security Common Units representing limited partner interests "
        "Trading Symbol GLP Security Exchange Name NYSE "
        "Title of 12(b) Security 9.50% Series B Fixed Rate Cumulative Redeemable "
        "Trading Symbol GLP pr B Security Exchange Name NYSE"
    )
    assert cover.securities(multi) == [
        {"title": "Common Units representing limited partner interests", "symbol": "GLP"},
        {"title": "9.50% Series B Fixed Rate Cumulative Redeemable", "symbol": "GLP pr B"},
    ]

    # LX's 20-F repeats the ADS symbol on the unlisted ordinary-share row. The
    # database can hold only one exact symbol, and the ADS is what NASDAQ prices.
    duplicate = (
        "Title of 12(b) Security American depositary shares (one American "
        "depositary share representing two Class A ordinary shares) "
        "Trading Symbol LX Security Exchange Name NASDAQ "
        "Title of 12(b) Security Class A ordinary shares* "
        "Trading Symbol LX Security Exchange Name NASDAQ"
    )
    assert cover.securities(duplicate) == [{
        "title": ("American depositary shares (one American depositary share "
                  "representing two Class A ordinary shares)"),
        "symbol": "LX",
    }]


def test_pending_foreign_annual_owns_the_cover_accession():
    row = {
        "data_pending": {"accession": "current20f", "filed": "2026-06-10"},
        "sources": {
            "eps": {"filed": "2025-06-18", "accn": "old20f"},
            "assets": {"filed": "2025-06-18", "accn": "old20f"},
        },
    }
    assert sync._cover_accession(row) == "current20f"

    del row["data_pending"]
    assert sync._cover_accession(row) == "old20f"


def _cover_sync_company(tmp_path, *, stored=False):
    cik = "0000001800"
    accession = "0001104659-26-043468"
    conn = store.connect(tmp_path / "cover.db")
    store.upsert_company(conn, cik, "ABT", "Abbott Laboratories")
    store.put_snapshot(conn, cik, "ok", {
        "cik": cik,
        "ticker": "ABT",
        "sources": {"assets": {"filed": "2026-02-20", "accn": accession}},
    })
    if stored:
        store.set_cover(conn, cik, [{
            "symbol": "ABT",
            "title": "Old parsed cover",
            "ratio": None,
        }], accession)
    conn.commit()
    return conn, cik, accession


class _CoverEdgar:
    def __init__(self, cache_dir, raw, *, fail=False):
        self.cache_dir = Path(cache_dir)
        self.raw = raw
        self.fail = fail
        self.requests = []
        self.submission_requests = []

    def _request(self, url):
        self.requests.append(url)
        if self.fail:
            raise RuntimeError("source unavailable")
        return SimpleNamespace(content=self.raw)

    def submissions(self, cik):
        self.submission_requests.append(cik)
        raise RuntimeError("submissions should not be needed")


class _ContinuityEdgar(_CoverEdgar):
    accession = "0001213900-26-103505"
    document = "issuer-f3.htm"

    def submissions(self, cik):
        self.submission_requests.append(cik)
        return {"filings": {"recent": {
            "filingDate": ["2026-09-25"],
            "form": ["F-3"],
            "accessionNumber": [self.accession],
            "primaryDocument": [self.document],
        }}}


def test_later_ticker_filing_is_retained_before_identity_parse(tmp_path, monkeypatch):
    cik = "0001901215"
    raw = (
        b"Ordinary Shares, no par value per share. Our Ordinary Shares are "
        b"listed on the Nasdaq Capital Market under the symbol BRNX."
    )
    edgar = _ContinuityEdgar(tmp_path / "cache", raw)
    path = cover.primary_document_cache_path(
        edgar.cache_dir, edgar.accession, edgar.document
    )
    resolve = sync.evidence.continuity_security

    def resolve_after_retention(*args):
        assert path.read_bytes() == raw
        return resolve(*args)

    monkeypatch.setattr(sync.evidence, "continuity_security", resolve_after_retention)
    proved = sync._later_ticker_continuity(
        edgar,
        cik,
        "BRNX",
        "Nasdaq",
        "2026-04-01",
        [{
            "symbol": "BNRG",
            "title": "Ordinary Shares, no par value per share",
            "ratio": None,
            "accn": "annual-1",
        }],
    )

    assert proved[:3] == (
        {
            "symbol": "BRNX",
            "previous_symbol": "BNRG",
            "title": "Ordinary Shares, no par value per share",
            "ratio": None,
            "accn": "annual-1",
            "exchange": "Nasdaq",
            "basis_accn": "annual-1",
        },
        edgar.accession,
        "2026-09-25",
    )
    assert proved[3] == path


def test_missing_later_filing_bytes_fail_closed(tmp_path):
    edgar = _ContinuityEdgar(tmp_path / "cache", b"", fail=True)

    assert sync._later_ticker_continuity(
        edgar,
        "0001901215",
        "BRNX",
        "Nasdaq",
        "2026-04-01",
        [{
            "symbol": "BNRG",
            "title": "Ordinary Shares, no par value per share",
            "ratio": None,
            "accn": "annual-1",
        }],
    ) is None
    assert not cover.primary_document_cache_path(
        edgar.cache_dir, edgar.accession, edgar.document
    ).exists()


@pytest.mark.parametrize("prior", [
    [],
    [{
        "symbol": "NOTE",
        "title": "5.000% Senior Notes due 2030",
        "ratio": None,
        "accn": "annual-1",
    }],
])
def test_later_filing_is_not_fetched_without_supported_annual_security(tmp_path, prior):
    edgar = _ContinuityEdgar(tmp_path / "cache", b"unused")

    assert sync._later_ticker_continuity(
        edgar,
        "0001901215",
        "BRNX",
        "Nasdaq",
        "2026-04-01",
        prior,
    ) is None
    assert edgar.submission_requests == []
    assert edgar.requests == []


def test_annual_reread_reselects_retained_continuity_without_sec_access(
    tmp_path, monkeypatch
):
    cik = "0001901215"
    annual_a = "0001213900-26-034046"
    annual_a2 = "0001213900-27-034046"
    later_l = "0001213900-26-103505"
    conn = store.connect(tmp_path / "continuity.db")
    store.upsert_company(conn, cik, "NEW", "Issuer")
    store.set_metadata(conn, cik, None, None, "Nasdaq", None)
    conn.execute("UPDATE company SET listed = 'y' WHERE cik = ?", (cik,))
    annual_security = {
        "symbol": "OLD",
        "title": "Common Shares, no par value",
        "ratio": None,
    }
    store.set_cover(conn, cik, [annual_security], annual_a, "2026-04-01")
    store.set_cover_continuity(conn, cik, {
        **annual_security,
        "previous_symbol": "OLD",
        "symbol": "NEW",
        "exchange": "Nasdaq",
        "basis_accn": annual_a,
    }, later_l, "2026-09-25")
    store.put_snapshot(conn, cik, "foreign", None)
    conn.commit()

    raw_a = (
        b"Title of 12(b) Security Common Shares, no par value "
        b"Trading Symbol OLD Security Exchange Name Nasdaq"
    )
    edgar = _CoverEdgar(tmp_path / "cache", b"", fail=True)
    facts = edgar.cache_dir / f"companyfacts_{cik}.json"
    facts.parent.mkdir(parents=True)
    facts.write_text("{}")
    path_a = cover.report_cache_path(edgar.cache_dir, annual_a, 1)
    path_a.parent.mkdir(parents=True)
    path_a.write_bytes(raw_a)
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)
    current = [annual_a, "2026-04-01"]
    monkeypatch.setattr(sync, "_current_supported_annual", lambda _facts: tuple(current))
    observations_before = [tuple(row) for row in conn.execute(
        """SELECT accn, symbol, title, exchange, ratio, filed, basis_accn, observed_at
           FROM security_cover_observation WHERE cik = ? ORDER BY accn, symbol""",
        (cik,),
    )]

    sync.cover_pages(
        conn, progress=lambda *_args: None, foreign_only=True,
        ciks={cik}, reparse=True,
    )

    assert edgar.requests == []
    assert edgar.submission_requests == []
    assert store.cover_for(conn, cik, "OLD") is None
    assert store.cover_for(conn, cik, "NEW")["accn"] == later_l
    assert [tuple(row) for row in conn.execute(
        """SELECT accn, symbol, title, exchange, ratio, filed, basis_accn, observed_at
           FROM security_cover_observation WHERE cik = ? ORDER BY accn, symbol""",
        (cik,),
    )] == observations_before
    assert store.needs_recompute(conn) == []

    raw_a2 = raw_a.replace(b"OLD", b"OLD2")
    path_a2 = cover.report_cache_path(edgar.cache_dir, annual_a2, 1)
    path_a2.parent.mkdir(parents=True)
    path_a2.write_bytes(raw_a2)
    current[:] = [annual_a2, "2027-04-01"]
    sync.cover_pages(
        conn, progress=lambda *_args: None, foreign_only=True,
        ciks={cik}, reparse=True,
    )

    assert edgar.requests == []
    assert edgar.submission_requests == [cik]
    assert store.cover_for(conn, cik, "NEW") is None
    assert store.cover_for(conn, cik, "OLD2")["accn"] == annual_a2
    assert store.needs_recompute(conn) == [cik]


def test_cover_reparse_requires_an_explicit_cik_set(tmp_path):
    conn, _, _ = _cover_sync_company(tmp_path)

    with pytest.raises(ValueError, match="explicit CIK set"):
        sync.cover_pages(conn, reparse=True)


def test_default_cover_sync_skips_a_covered_immutable_accession(
    tmp_path, monkeypatch
):
    conn, _, _ = _cover_sync_company(tmp_path, stored=True)
    conn.execute("UPDATE security_cover SET symbol = 'ABT/ALT'")
    conn.commit()
    edgar = _CoverEdgar(tmp_path / "cache", b"unused")
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)

    sync.cover_pages(conn, progress=lambda *_args: None)

    assert edgar.requests == []


def test_cover_reparse_atomically_caches_exact_bytes_and_reuses_them(
    tmp_path, monkeypatch
):
    conn, cik, accession = _cover_sync_company(tmp_path, stored=True)
    raw = (
        b"Title of 12(b) Security Common Shares, Without Par Value "
        b"Trading Symbol ABT Security Exchange Name NYSE"
    )
    edgar = _CoverEdgar(tmp_path / "cache", raw)
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)
    path = cover.report_cache_path(edgar.cache_dir, accession, 1)
    parse = cover.securities

    def parse_after_retention(document):
        assert path.read_bytes() == raw
        return parse(document)

    monkeypatch.setattr(cover, "securities", parse_after_retention)

    sync.cover_pages(
        conn, progress=lambda *_args: None, ciks={cik}, reparse=True
    )

    assert len(edgar.requests) == 1
    assert store.cover_for(conn, cik, "ABT")["title"] == (
        "Common Shares, Without Par Value"
    )

    edgar.fail = True
    store.set_cover(conn, cik, [{
        "symbol": "ABT",
        "title": "Stale parsed cover",
        "ratio": None,
    }], accession)
    sync.cover_pages(
        conn, progress=lambda *_args: None, ciks={cik}, reparse=True
    )

    assert len(edgar.requests) == 1
    assert store.cover_for(conn, cik, "ABT")["title"] == (
        "Common Shares, Without Par Value"
    )


def test_failed_cover_fetch_keeps_existing_parse_and_creates_no_cache(
    tmp_path, monkeypatch
):
    conn, cik, accession = _cover_sync_company(tmp_path, stored=True)
    edgar = _CoverEdgar(tmp_path / "cache", b"", fail=True)
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)

    sync.cover_pages(
        conn, progress=lambda *_args: None, ciks={cik}, reparse=True
    )

    assert len(edgar.requests) == len(cover.COVER_REPORTS)
    assert store.cover_for(conn, cik, "ABT")["title"] == "Old parsed cover"
    assert not any(
        cover.report_cache_path(edgar.cache_dir, accession, report).exists()
        for report in cover.COVER_REPORTS
    )


def test_cached_foreign_reparse_makes_no_request_and_keeps_stored_ratio(
    tmp_path, monkeypatch
):
    cik = "0001110646"
    accession = "0001104659-26-043468"
    conn = store.connect(tmp_path / "foreign-cover.db")
    store.upsert_company(conn, cik, "NTES", "NetEase")
    conn.execute("UPDATE company SET listed = 'y' WHERE cik = ?", (cik,))
    store.put_snapshot(conn, cik, "foreign", None)
    store.set_cover(conn, cik, [{
        "symbol": "NTES",
        "title": "American Depositary Shares",
        "ratio": Decimal("5"),
    }], accession)
    conn.commit()

    raw = (
        b"Title of 12(b) Security American Depositary Shares "
        b"Trading Symbol NTES Security Exchange Name NASDAQ"
    )
    edgar = _CoverEdgar(tmp_path / "cache", raw)
    facts = edgar.cache_dir / f"companyfacts_{cik}.json"
    facts.parent.mkdir(parents=True)
    facts.write_text("{}")
    path = cover.report_cache_path(edgar.cache_dir, accession, 2)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)
    monkeypatch.setattr(
        sync,
        "_current_supported_annual",
        lambda _facts: (accession, "2026-02-20"),
    )

    sync.cover_pages(
        conn,
        progress=lambda *_args: None,
        foreign_only=True,
        ciks={cik},
        reparse=True,
    )

    assert edgar.requests == []
    assert edgar.submission_requests == []
    assert store.cover_for(conn, cik, "NTES")["ratio"] == "5"


def _foreign_cover_reparse(tmp_path, monkeypatch, raw_report, raw_primary=b""):
    cik = "0001333141"
    ticker = "FMS"
    accession = "0001104659-26-019005"
    primary_document = "fms-20251231x20f.htm"
    conn = store.connect(tmp_path / "foreign-ratio.db")
    store.upsert_company(conn, cik, ticker, "Fresenius Medical Care")
    conn.execute("UPDATE company SET listed = 'y' WHERE cik = ?", (cik,))
    store.put_snapshot(conn, cik, "foreign", None)
    conn.commit()

    class Edgar(_CoverEdgar):
        def submissions(self, requested_cik):
            self.submission_requests.append(requested_cik)
            return {
                "filings": {
                    "recent": {
                        "accessionNumber": [accession],
                        "primaryDocument": [primary_document],
                        "filingDate": ["2026-02-24"],
                    }
                }
            }

    edgar = Edgar(tmp_path / "cache", raw_primary)
    facts = edgar.cache_dir / f"companyfacts_{cik}.json"
    facts.parent.mkdir(parents=True)
    facts.write_text("{}")
    report_path = cover.report_cache_path(edgar.cache_dir, accession, 1)
    report_path.parent.mkdir(parents=True)
    report_path.write_bytes(raw_report)
    monkeypatch.setattr(sync, "EdgarClient", lambda: edgar)
    monkeypatch.setattr(
        sync,
        "_current_supported_annual",
        lambda _facts: (accession, "2026-02-24"),
    )
    return conn, edgar, cik, ticker, accession, primary_document


def test_retained_r_report_can_supply_one_exact_unresolved_class(
    tmp_path, monkeypatch
):
    raw = (
        b"<table><tr><td>Title of 12(b) Security</td><td>American Depositary "
        b"Shares</td></tr><tr><td>Trading Symbol</td><td>FMS</td></tr></table>"
        b" Where ADSs are held, two ADSs represent one ordinary share."
    )
    conn, edgar, cik, ticker, _, _ = _foreign_cover_reparse(
        tmp_path, monkeypatch, raw
    )

    sync.cover_pages(
        conn,
        progress=lambda *_args: None,
        foreign_only=True,
        ciks={cik},
        reparse=True,
    )

    assert edgar.requests == []
    assert edgar.submission_requests == []
    assert store.cover_for(conn, cik, ticker)["ratio"] == "0.5"


def test_primary_document_is_retained_before_ratio_parse_and_reused(
    tmp_path, monkeypatch
):
    raw_report = (
        b"<table><tr><td>Title of 12(b) Security</td><td>American Depositary "
        b"Shares</td></tr><tr><td>Trading Symbol</td><td>FMS</td></tr></table>"
    )
    raw_primary = b"Where ADSs are held, two ADSs represent one ordinary share."
    conn, edgar, cik, ticker, accession, primary_document = _foreign_cover_reparse(
        tmp_path, monkeypatch, raw_report, raw_primary
    )
    primary_path = cover.primary_document_cache_path(
        edgar.cache_dir, accession, primary_document
    )
    parse = cover.depositary_ratio

    def parse_after_retention(text):
        if "two ADSs represent" in text:
            assert primary_path.read_bytes() == raw_primary
        return parse(text)

    monkeypatch.setattr(cover, "depositary_ratio", parse_after_retention)

    sync.cover_pages(
        conn,
        progress=lambda *_args: None,
        foreign_only=True,
        ciks={cik},
        reparse=True,
    )

    assert len(edgar.requests) == 1
    assert store.cover_for(conn, cik, ticker)["ratio"] == "0.5"

    conn.execute(
        "UPDATE security_cover SET ratio = NULL WHERE cik = ? AND symbol = ?",
        (cik, ticker),
    )
    conn.commit()
    edgar.fail = True
    edgar.requests.clear()
    edgar.submission_requests.clear()
    sync.cover_pages(
        conn,
        progress=lambda *_args: None,
        foreign_only=True,
        ciks={cik},
        reparse=True,
    )

    assert edgar.requests == []
    assert edgar.submission_requests == []
    assert store.cover_for(conn, cik, ticker)["ratio"] == "0.5"


def test_conflicting_primary_document_relations_remain_unresolved(
    tmp_path, monkeypatch
):
    raw_report = (
        b"<table><tr><td>Title of 12(b) Security</td><td>American Depositary "
        b"Shares</td></tr><tr><td>Trading Symbol</td><td>FMS</td></tr></table>"
    )
    raw_primary = (
        b"Each ADS represents eight common shares. Historical ADSs each "
        b"represented one common share."
    )
    conn, _, cik, ticker, _, _ = _foreign_cover_reparse(
        tmp_path, monkeypatch, raw_report, raw_primary
    )

    sync.cover_pages(
        conn,
        progress=lambda *_args: None,
        foreign_only=True,
        ciks={cik},
        reparse=True,
    )

    assert store.cover_for(conn, cik, ticker)["ratio"] is None


def test_an_award_total_says_which_kinds_it_contains():
    """A company that tags only options has said nothing about its restricted stock,
    so the total names its own basis rather than implying the other half is zero."""
    from decimal import Decimal
    from datetime import date
    from screener.sync import _equity_awards
    from screener.models import Fact, Provenance

    def fact(v):
        return Fact(value=Decimal(v), provenance=Provenance(
            concept="c", tag="t", fiscal_year=2026, form="10-K",
            accession="a", filed=date(2026, 2, 1), period_end=date(2025, 12, 31)))

    class Snap:
        options_outstanding = fact(6e6)
        rsus_outstanding = fact(3e6)
    assert _equity_awards(Snap()) == {"equity_awards": 9e6, "awards_basis": "options + RSUs"}

    class OptionsOnly:
        options_outstanding = fact(6e6)
        rsus_outstanding = None
    assert _equity_awards(OptionsOnly()) == {"equity_awards": 6e6, "awards_basis": "options only"}

    class Neither:
        options_outstanding = rsus_outstanding = None
    assert _equity_awards(Neither()) == {"equity_awards": None, "awards_basis": None}


def test_the_regression_harness_ignores_only_live_inputs_it_cannot_reproduce():
    """The harness exists because three hand-written comparison scripts were wrong
    before it — they left out the dimensioned sidecar, the cover ratio, or the
    export-time enrichment, and each reported healthy companies as broken. So the
    fields a recomputation cannot reproduce are named in one place rather than
    rediscovered every time. Price-settled results are reproducible from the exact
    shipped quote and must not be carried over from the baseline."""
    from screener.regress import VOLATILE, _flat, _moved

    assert "price" in VOLATILE and "ttm_eps_vintage" in VOLATILE
    # nested figures are attributed to their own field, not to the whole object
    assert _flat({"annual_eps": {"2020": 1.76}, "price": 9,
                  "criteria": [{"n": 1, "status": "PASS"}]}) == {
        "annual_eps.2020": 1.76,
        "criteria": '[{"n": 1, "status": "PASS"}]',
    }
    # a rounded last decimal is not a change; a sign flip is
    assert not _moved(1.7600, 1.76001)
    assert _moved(0.64, -0.64)


def test_a_printed_statement_is_read_at_the_scale_its_header_declares():
    """"$ in Millions" over a cell reading 45,468 means 45.468 billion dollars.
    Reading the cell as dollars is the difference between a company and a corner
    shop, and it is the one mistake a statement reader can make silently."""
    from screener.sources import statements
    doc = """<html><body><p>CONSOLIDATED BALANCE SHEETS - USD ($) $ in Millions</p>
    <table><tr><td>Total assets</td><td>104,217</td><td>100,549</td></tr>
    <tr><td>Accumulated deficit</td><td>(1,234)</td><td>(999)</td></tr></table></body></html>"""
    assert statements.scale(doc) == 1_000_000
    lines = statements.lines(doc)
    assert statements.value_for(lines, "total assets")[1] == 104_217_000_000
    # a parenthesised cell is negative, and the comparative column is ignored
    assert statements.value_for(lines, "accumulated deficit")[1] == -1_234_000_000


def test_statement_dates_come_only_from_header_rows():
    """A later caption can repeat both dates in an order unrelated to columns."""
    from screener.sources import statements
    doc = """<table class="report">
      <tr><th>Statement</th><th>6 Months Ended</th><th>12 Months Ended</th></tr>
      <tr><th></th><th>Dec. 31, 2024</th><th>Dec. 31, 2025</th></tr>
      <tr><td>Revenue</td><td>120,775</td><td>204,055</td></tr>
      <tr><td>Expense (for the year ended December 31, 2025 and the period ended
          December 31, 2024)</td><td>(1)</td><td>(2)</td></tr>
    </table>"""

    assert statements.columns(doc) == [date(2024, 12, 31), date(2025, 12, 31)]


def test_all_blank_scenario_column_does_not_shift_statement_values():
    """CCI heads an empty future-date scenario before its two real columns."""
    from screener.sources import statements
    doc = """<table class="report">
      <tr><th>Balance Sheet</th><th>Dec. 31, 2026</th>
          <th>Jun. 30, 2026</th><th>Dec. 31, 2025</th></tr>
      <tr><td><a onclick="defref_us-gaap_Assets">Total assets</a></td>
          <td></td><td>21,512</td><td>31,518</td></tr>
      <tr><td>Current assets</td><td></td><td>1,716</td><td>1,144</td></tr>
    </table>"""

    assert statements.columns(doc) == [date(2026, 6, 30), date(2025, 12, 31)]
    assert statements.value_for(statements.lines(doc), "total assets")[1] == 21_512
    assert statements.elements(doc)["us-gaap_Assets"] == [
        Decimal("21512"), Decimal("31518")]


def test_empty_rendered_label_does_not_shift_tagged_element_columns():
    """Old SEC renderers sometimes link a concept on a row with no caption."""
    from screener.sources import statements
    doc = """<table class="report">
      <tr><th>Operations</th><th>Jul. 31, 2012</th>
          <th>Jul. 31, 2011</th><th>Jul. 31, 2012</th></tr>
      <tr><td><a onclick="defref_us-gaap_GrossProfit"></a></td>
          <td>15,314</td><td>0</td><td>15,314</td></tr>
    </table>"""

    assert statements.elements(doc)["us-gaap_GrossProfit"] == [
        Decimal("15314"), Decimal("0"), Decimal("15314")]


def test_a_statement_is_found_by_its_printed_name_not_its_number():
    """R-numbers differ between filers and between years; the name does not. The
    parenthetical companion carries share counts, not the statement."""
    from screener.sources import statements
    summary = """<Reports>
      <Report><HtmlFileName>R2.htm</HtmlFileName><ShortName>CONDENSED CONSOLIDATED STATEMENTS OF INCOME</ShortName></Report>
      <Report><HtmlFileName>R4.htm</HtmlFileName><ShortName>CONDENSED CONSOLIDATED BALANCE SHEETS</ShortName></Report>
      <Report><HtmlFileName>R5.htm</HtmlFileName><ShortName>CONDENSED CONSOLIDATED BALANCE SHEETS PARENTHETICAL</ShortName></Report>
    </Reports>"""
    assert statements.find(summary, "balance_sheet") == "R4.htm"
    assert statements.find(summary, "income") == "R2.htm"


def test_total_liabilities_is_not_total_liabilities_and_equity():
    """Coca-Cola prints no total-liabilities line at all. Matching that label by
    substring would take "Total Liabilities and Equity" — its total assets — and
    call the company solvent to the last dollar."""
    from screener.sources import statements
    doc = """<html><body><p>$ in Millions</p><table>
      <tr><td>Total Equity</td><td>35,734</td></tr>
      <tr><td>Total Liabilities and Equity</td><td>104,217</td></tr></table></body></html>"""
    lines = statements.lines(doc)
    assert statements.value_for(lines, "total liabilities") is None


def test_the_money_scale_is_the_one_attached_to_the_dollar_sign():
    """CoStar's income statement is headed "$ in Thousands, shares in Millions".
    A pattern that looks for the word "millions" finds the share clause and reads
    every dollar figure a thousand times too large — its $7.0M of net income
    became $7.0bn."""
    from screener.sources import statements
    header = ("<html><body><p>CONSOLIDATED STATEMENTS OF OPERATIONS - USD ($) "
              "$ in Thousands, shares in Millions</p><table>"
              "<tr><td>Net income</td><td>7,000</td></tr>"
              "<tr><td>Weighted average shares outstanding</td><td>410</td></tr>"
              "</table></body></html>")
    assert statements.scale(header) == 1_000
    lines = statements.lines(header)
    assert statements.value_for(lines, "net income")[1] == 7_000_000
    # the share row takes the share scale, not the money one
    assert statements.value_for(lines, "weighted average shares outstanding")[1] == 410_000_000


def test_the_parents_share_of_profit_wins_over_the_consolidated_line():
    """Apollo prints $5,401M of consolidated net income and $3,492M attributable to
    itself. `NetIncomeLoss` is the second, and every per-share figure divides it."""
    from screener.sources import statements
    doc = ("<html><body><p>$ in Millions</p><table>"
           "<tr><td>Net income (loss)</td><td>5,401</td></tr>"
           "<tr><td>Net income (loss) attributable to noncontrolling interests</td><td>1,909</td></tr>"
           "<tr><td>Net income (loss) attributable to Apollo Global Management, Inc.</td><td>3,492</td></tr>"
           "</table></body></html>")
    lines = statements.lines(doc)
    hit = statements.value_for(lines, "net income (loss) attributable to*", "net income (loss)")
    assert hit[1] == 3_492_000_000        # not the minority holders' 1,909


def test_a_loss_is_read_whichever_side_the_dollar_sign_falls():
    """SEC renders a negative as "$ (52)" — dollar sign outside the bracket. A
    pattern expecting "(" first skips the cell silently and reads the NEXT column
    instead, which is the prior year: Hyatt's $52M loss was read as the $1,296M
    profit it made the year before."""
    from screener.sources import statements
    doc = ("<html><body><p>$ in Millions</p><table>"
           "<tr><td>Net income (loss) attributable to Hyatt Hotels Corporation</td>"
           "<td>$ (52)</td><td>$ 1,296</td><td>$ 220</td></tr></table></body></html>")
    lines = statements.lines(doc)
    assert statements.value_for(lines, "net income (loss) attributable to*")[1] == -52_000_000
    # the later columns are still available, in the order the filer printed them
    assert lines[0][1] == [-52_000_000, 1_296_000_000, 220_000_000]


def test_a_section_heading_is_not_the_total_it_introduces():
    """An income statement opens "Revenues:" as the heading of the section that
    follows, and a heading row can carry a stray figure. Matching the bare word
    first read Robinhood's revenue as $2,628M against a $4,473M year, so every
    "total" form is tried before any bare one."""
    from screener.sources import statements
    doc = ("<html><body><p>$ in Millions</p><table>"
           "<tr><td>Revenues</td><td>2,628</td></tr>"
           "<tr><td>Other revenues</td><td>1,845</td></tr>"
           "<tr><td>Total net revenues</td><td>4,473</td></tr></table></body></html>")
    lines = statements.lines(doc)
    hit = statements.value_for(lines, "total net revenues", "total revenues", "revenues")
    assert hit[1] == 4_473_000_000


def test_the_common_stockholders_line_is_the_parents_own():
    """Cohen & Steers and Vivid Seats' Class A both name the parent's share that
    way. Excluding the phrase took the consolidated figure instead — $157.4M where
    the company earned $153.2M."""
    from screener.sources import statements
    doc = ("<html><body><p>$ in Thousands</p><table>"
           "<tr><td>Net income</td><td>157,398</td></tr>"
           "<tr><td>Net income attributable to redeemable noncontrolling interests</td><td>4,181</td></tr>"
           "<tr><td>Net income attributable to common stockholders</td><td>153,217</td></tr>"
           "</table></body></html>")
    lines = statements.lines(doc)
    # the phrase is matched explicitly and late, never by the prefix: Uniti uses the
    # same words for its figure after preferred dividends
    hit = statements.value_for(lines, "net income attributable to*",
                               "net income attributable to common stockholders",
                               "net income")
    assert hit[1] == 153_217_000       # not the minority holders' 4,181


def test_a_shareholder_is_not_a_share():
    """"Net Income to Shareholders" contains the word "share" and is money. Scaling
    it as a share count left Markel's $2.1bn of profit reading as $2,107,010, which
    is small enough to look like a plausible figure and be believed."""
    from screener.sources import statements
    doc = ("<html><body><p>Shares in Thousands, $ in Thousands</p><table>"
           "<tr><td>Net Income to Shareholders</td><td>2,107,010</td></tr>"
           "<tr><td>Weighted average shares outstanding</td><td>12,600</td></tr>"
           "</table></body></html>")
    lines = statements.lines(doc)
    assert statements.value_for(lines, "net income to shareholders")[1] == 2_107_010_000
    assert statements.value_for(lines, "weighted average shares outstanding")[1] == 12_600_000


def test_a_missing_half_of_a_derived_figure_is_not_zero():
    """Isabella Bank's combined intangibles line and its goodwill are both
    $48,282,000, so intangibles excluding goodwill are correctly 0 — and the
    goodwill for that date is carried by an earlier accession than the combined
    line. Substituting zero for the half it could not find read the whole combined
    figure as intangible and called a correct answer an error."""
    from screener.audit import _fact_in_filing
    facts = {"facts": {"us-gaap": {
        "IntangibleAssetsNetIncludingGoodwill": {"units": {"USD": [
            {"end": "2025-12-31", "val": 48282000, "accn": "q3", "filed": "2026-08-10"}]}},
        "Goodwill": {"units": {"USD": [
            {"end": "2025-12-31", "val": 48282000, "accn": "k25", "filed": "2026-02-20"}]}},
    }}}
    source = {"tag": "us-gaap:IntangibleAssetsNetIncludingGoodwill - us-gaap:Goodwill",
              "accn": "q3", "end": "2025-12-31"}
    assert _fact_in_filing(facts, source) == 0

    missing = {"tag": "us-gaap:IntangibleAssetsNetIncludingGoodwill - us-gaap:NotFiled",
               "accn": "q3", "end": "2025-12-31"}
    assert _fact_in_filing(facts, missing) is None      # unknown, never 0


def test_a_weighted_average_has_several_values_for_one_end_date():
    """A quarterly report states the three-month average, the six-month and the year
    to date, all ending on the same day, and the provenance records only the end. The
    honest question is whether the displayed figure is one of the numbers the filing
    states, not whether it is the first of them."""
    from screener.audit import _values_in_filing
    facts = {"facts": {"us-gaap": {"WeightedAverageNumberOfDilutedSharesOutstanding": {
        "units": {"shares": [
            {"end": "2026-06-30", "val": 193323374, "accn": "q2", "start": "2026-04-01"},
            {"end": "2026-06-30", "val": 75870706, "accn": "q2", "start": "2026-01-01"},
        ]}}}}}
    found = _values_in_filing(facts, "us-gaap",
                              "WeightedAverageNumberOfDilutedSharesOutstanding",
                              {"accn": "q2", "end": "2026-06-30"})
    assert sorted(found) == [75870706, 193323374]


def test_the_parentheses_carry_the_sign_and_the_label_never_does():
    """Intellicheck heads its line "Net loss" and prints "$ 1,273" beside a prior
    year of "$ (918)" — a stale caption over a real profit, which the page's own
    arithmetic confirms: pre-tax 1,331 less tax 58 is 1,273. Reading the caption as
    a sign turned a profitable year into a loss."""
    from screener.sources import statements
    doc = ("<html><body><p>$ in Thousands</p><table>"
           "<tr><td>Net income (loss) before provision for income taxes</td>"
           "<td>1,331</td><td>(885)</td></tr>"
           "<tr><td>Provision for income taxes</td><td>58</td><td>33</td></tr>"
           "<tr><td>Net loss</td><td>$ 1,273</td><td>$ (918)</td></tr></table></body></html>")
    lines = statements.lines(doc)
    assert statements.value_for(lines, "net loss")[1] == 1_273_000
    assert statements.value_for(lines, "net loss", column=1)[1] == -918_000


def test_a_note_is_never_mistaken_for_the_statement_itself():
    """Creatd titles its income statement "Consolidated Statements of Comprehensive
    Loss", which no operations-shaped phrase matches, so the search ran on and
    reached a note eighty reports later that happened to mention operations. SEC
    marks the primary statements with MenuCategory, which settles it outright."""
    from screener.sources import statements
    summary = """<Reports>
      <Report><HtmlFileName>R2.htm</HtmlFileName><ShortName>Consolidated Balance Sheets</ShortName><MenuCategory>Statements</MenuCategory></Report>
      <Report><HtmlFileName>R4.htm</HtmlFileName><ShortName>Consolidated Statements of Comprehensive Loss</ShortName><MenuCategory>Statements</MenuCategory></Report>
      <Report><HtmlFileName>R71.htm</HtmlFileName><ShortName>Segment Results of Operations (Details)</ShortName><MenuCategory>Details</MenuCategory></Report>
    </Reports>"""
    assert statements.find(summary, "income") == "R4.htm"
    assert statements.find(summary, "balance_sheet") == "R2.htm"


def test_a_pure_operations_statement_outranks_a_combined_one():
    """A filer publishing both keeps the income statement, not the comprehensive one:
    comprehensive income carries currency and pension movements that are not
    earnings."""
    from screener.sources import statements
    summary = """<Reports>
      <Report><HtmlFileName>R2.htm</HtmlFileName><ShortName>Consolidated Statements of Operations</ShortName><MenuCategory>Statements</MenuCategory></Report>
      <Report><HtmlFileName>R3.htm</HtmlFileName><ShortName>Consolidated Statements of Comprehensive Income</ShortName><MenuCategory>Statements</MenuCategory></Report>
    </Reports>"""
    assert statements.find(summary, "income") == "R2.htm"


def test_a_registration_statement_does_not_supersede_a_periodic_report():
    """Cycurion's line of credit has a newer figure than the one on the panel, but it
    stands in an S-1 — a registration statement, not a quarterly or annual report —
    and the engine reads neither. A figure cannot be stale against something the
    engine was never going to see."""
    from screener.audit import _one_moment
    facts = {"facts": {"us-gaap": {"LineOfCredit": {"units": {"USD": [
        {"end": "2025-12-31", "val": 2933396, "accn": "k25", "form": "10-K"},
        {"end": "2026-03-31", "val": 2700000, "accn": "s1", "form": "S-1"},
    ]}}}}}
    row = {"sources": {"long_term_debt": {
        "end": "2026-06-30",
        "components": [{"tag": "us-gaap:LineOfCredit", "end": "2025-12-31"}]}}}
    assert _one_moment(row, facts) == []

    # ...but a 10-Q carrying the same period does supersede it
    facts["facts"]["us-gaap"]["LineOfCredit"]["units"]["USD"][1]["form"] = "10-Q"
    assert len(_one_moment(row, facts)) == 1


def test_a_newer_non_usd_fact_does_not_supersede_the_selected_usd_component():
    from screener.audit import _one_moment
    facts = {"facts": {"us-gaap": {"FinanceLeaseLiabilityCurrent": {"units": {
        "USD": [{"end": "2024-12-31", "val": 10, "accn": "a", "form": "20-F"}],
        "CNY": [{"end": "2025-12-31", "val": 70, "accn": "b", "form": "20-F"}],
    }}}}}
    row = {"sources": {"short_term_debt": {
        "end": "2025-12-31",
        "components": [{"tag": "us-gaap:FinanceLeaseLiabilityCurrent",
                        "end": "2024-12-31"}],
    }}}

    assert _one_moment(row, facts) == []


def test_a_declared_scale_the_statement_contradicts_is_not_a_wrong_figure():
    """ABVC heads its balance sheet "$ in Millions" and then prints cash of
    "$ 31,944", which at millions would be $31.9 trillion. The tagged values carry
    no such ambiguity, so a difference of exactly a million is the header being
    wrong, not the panel."""
    from screener.audit import _only_the_scale_differs
    assert _only_the_scale_differs(19_400_715, 19_400_715_000_000) is True
    assert _only_the_scale_differs(24_246_000, 24_049_000) is False    # a real difference
    assert _only_the_scale_differs(0, 5) is False


class _InlineEdgar:
    def __init__(self, cache_dir, *, include_instance=True, unsafe=False, fail=None):
        self.cache_dir = cache_dir
        self.calls = []
        self.fail = fail
        names = [
            "annual.htm", "FilingSummary.xml", "issuer.xsd", "issuer_pre.xml",
        ]
        if include_instance:
            names.append("annual_htm.xml")
        if unsafe:
            names.append("../escape.xml")
        self.index = json.dumps({
            "directory": {"item": [{"name": name} for name in names]},
        }).encode()

    def submissions(self, cik):
        return {
            "name": "Example PLC",
            "filings": {"recent": {
                "accessionNumber": ["0000000001-26-000001"],
                "form": ["20-F"],
                "filingDate": ["2026-03-01"],
                "reportDate": ["2025-12-31"],
                "primaryDocument": ["annual.htm"],
            }},
        }

    def _request(self, url):
        self.calls.append(url)
        if self.fail and url.endswith(self.fail):
            raise RuntimeError("fetch failed")
        return SimpleNamespace(
            content=(self.index if url.endswith("index.json") else url.encode()),
        )


class _IncorporatedInlineEdgar:
    annual_accession = "0000000001-26-000010"
    source_accession = "0000000001-26-000009"

    def __init__(
        self, cache_dir, *, linked_cik="1", linked_accession=None,
        linked_document="statements.htm", source_document="statements.htm",
        source_form="6-K", source_filed="2026-03-01", second_link=False,
        fail=None,
    ):
        self.cache_dir = cache_dir
        self.fail = fail
        self.linked_accession = linked_accession or self.source_accession
        linked_digits = self.linked_accession.replace("-", "")
        other = (
            '<a href="https://www.sec.gov/Archives/edgar/data/1/'
            '000000000126000008/other.htm">Audited annual financial statements</a>'
            if second_link else ""
        )
        self.annual_html = (
            '<html><a href="https://www.sec.gov/Archives/edgar/data/'
            f'{linked_cik}/{linked_digits}/{linked_document}">'
            'Audited annual consolidated financial statements</a>'
            f'{other}<p>Incorporated by reference from the registrant\'s Form 6-K.</p>'
            '</html>'
        ).encode()
        self.annual_index = json.dumps({
            "directory": {"item": [{"name": "annual.htm"}]},
        }).encode()
        self.source_index = json.dumps({
            "directory": {"item": [{"name": name} for name in (
                source_document, "statements_htm.xml", "FilingSummary.xml",
                "issuer.xsd", "issuer_pre.xml",
            )]},
        }).encode()
        self.source_document = source_document
        self.source_form = source_form
        self.source_filed = source_filed

    def submissions(self, cik):
        return {
            "name": "Example PLC",
            "filings": {"recent": {
                "accessionNumber": [self.annual_accession, self.source_accession],
                "form": ["40-F", self.source_form],
                "filingDate": ["2026-03-01", self.source_filed],
                "reportDate": ["2025-12-31", "2025-12-31"],
                "primaryDocument": ["annual.htm", self.source_document],
            }},
        }

    def _request(self, url):
        if self.fail and url.endswith(self.fail):
            raise RuntimeError("fetch failed")
        if url.endswith("index.json"):
            raw = (self.source_index if self.source_accession.replace("-", "") in url
                   else self.annual_index)
        elif url.endswith("annual.htm"):
            raw = self.annual_html
        else:
            raw = url.encode()
        return SimpleNamespace(content=raw)


def test_inline_acquisition_requires_explicit_bounded_ciks(tmp_path):
    conn = store.connect(tmp_path / "store.db")

    with pytest.raises(ValueError, match="explicit CIK set"):
        sync.retain_inline_statements(conn, set(), edgar=_InlineEdgar(tmp_path))


def test_inline_acquisition_publishes_after_all_bytes_and_is_idempotent(
    tmp_path, monkeypatch,
):
    conn = store.connect(tmp_path / "store.db")
    edgar = _InlineEdgar(tmp_path)
    verified = []

    def verify(manifest, cache_dir, cik, **kwargs):
        if not verified:
            assert not inline_xbrl.current_manifest_path(cache_dir, cik).exists()
        directory = inline_xbrl.accession_directory(
            cache_dir, manifest["annual"]["accession"]
        )
        assert all((directory / record["document"]).exists()
                   for record in manifest["files"].values())
        verified.append(cik)
        return {}

    monkeypatch.setattr(sync, "_verified_inline_manifest", verify)
    first = sync.retain_inline_statements(
        conn, {"1"}, progress=lambda *args: None, edgar=edgar,
    )

    assert first["0000000001"]["state"] == "activated"
    assert verified == ["0000000001"]
    assert conn.execute("SELECT reason FROM snapshot_dirty").fetchone()["reason"].endswith(
        "0000000001-26-000001"
    )
    assert all(url.startswith("https://www.sec.gov/Archives/edgar/data/1/")
               for url in edgar.calls)

    conn.execute("DELETE FROM snapshot_dirty")
    conn.commit()
    second = sync.retain_inline_statements(
        conn, {"0000000001"}, progress=lambda *args: None, edgar=edgar,
    )
    assert second["0000000001"]["state"] == "reused"
    assert conn.execute("SELECT * FROM snapshot_dirty").fetchone() is None


def test_inline_acquisition_publishes_one_verified_incorporated_sec_exhibit(
    tmp_path, monkeypatch,
):
    conn = store.connect(tmp_path / "store.db")
    edgar = _IncorporatedInlineEdgar(tmp_path)
    seen = []

    def verify(manifest, cache_dir, cik):
        seen.append(manifest)
        assert manifest["relationship"] == "incorporated_annual_exhibit"
        assert manifest["annual"]["accession"] == edgar.annual_accession
        assert manifest["source"]["accession"] == edgar.source_accession
        assert set(manifest["files"]) == {
            "annual_index", "annual_primary_document", "source_index",
            "instance", "filing_summary", "presentation", "schema",
            "primary_document",
        }
        for role, record in manifest["files"].items():
            accession = (edgar.annual_accession if role.startswith("annual_")
                         else edgar.source_accession)
            directory = inline_xbrl.accession_directory(cache_dir, accession)
            assert (directory / record["document"]).exists()
        return {"facts": {}}

    monkeypatch.setattr(sync, "_verified_inline_manifest", verify)

    result = sync.retain_inline_statements(
        conn, {"1"}, progress=lambda *args: None, edgar=edgar,
    )

    assert result["0000000001"] == {
        "state": "activated", "accession": edgar.annual_accession,
    }
    assert len(seen) == 1
    persisted = inline_xbrl.read_current_manifest(tmp_path, "1")
    assert persisted["relationship_evidence"]["document"] == "statements.htm"


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"linked_cik": "2"}, "ambiguous_incorporated_statement_links:0"),
    ({"linked_accession": "0000000001-26-000099"}, "absent or ambiguous"),
    ({"linked_document": "other.htm"}, "does not match submissions"),
    ({"source_form": "8-K"}, "does not match Form 6-K"),
    ({"source_filed": "2026-03-02"}, "later than annual wrapper"),
    ({"second_link": True}, "ambiguous_incorporated_statement_links:2"),
])
def test_incorporated_acquisition_rejects_identity_and_relationship_mismatch(
    tmp_path, kwargs, message,
):
    conn = store.connect(tmp_path / "store.db")
    result = sync.retain_inline_statements(
        conn, {"1"}, progress=lambda *args: None,
        edgar=_IncorporatedInlineEdgar(tmp_path, **kwargs),
    )

    row = result["0000000001"]
    assert message in (row.get("error") or row.get("relationship") or "")
    assert not inline_xbrl.current_manifest_path(tmp_path, "1").exists()


def test_partial_incorporated_fetch_never_publishes_a_manifest(tmp_path):
    conn = store.connect(tmp_path / "store.db")
    result = sync.retain_inline_statements(
        conn, {"1"}, progress=lambda *args: None,
        edgar=_IncorporatedInlineEdgar(tmp_path, fail="issuer.xsd"),
    )

    assert result["0000000001"]["state"] == "error"
    assert not inline_xbrl.current_manifest_path(tmp_path, "1").exists()


def test_partial_inline_fetch_never_publishes_a_manifest(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "store.db")
    edgar = _InlineEdgar(tmp_path, fail="issuer.xsd")
    monkeypatch.setattr(
        inline_xbrl, "verify_manifest",
        lambda *args, **kwargs: pytest.fail("partial source must not parse"),
    )

    result = sync.retain_inline_statements(
        conn, {"0000000001"}, progress=lambda *args: None, edgar=edgar,
    )

    assert result["0000000001"]["state"] == "error"
    assert not inline_xbrl.current_manifest_path(tmp_path, "0000000001").exists()
    assert conn.execute("SELECT * FROM snapshot_dirty").fetchone() is None


def test_inline_job_rejects_missing_incorporation_evidence(
    tmp_path, monkeypatch,
):
    conn = store.connect(tmp_path / "store.db")
    edgar = _InlineEdgar(tmp_path, include_instance=False)
    monkeypatch.setattr(
        inline_xbrl, "verify_manifest",
        lambda *args, **kwargs: pytest.fail("unsupported source must not parse"),
    )

    result = sync.retain_inline_statements(
        conn, {"0000000001"}, progress=lambda *args: None, edgar=edgar,
    )

    assert result["0000000001"] == {
        "state": "unsupported_relationship",
        "relationship": "incorporated_filing_relationship",
    }
    assert not inline_xbrl.current_manifest_path(tmp_path, "0000000001").exists()


def test_inline_index_rejects_unsafe_archive_paths(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "store.db")
    edgar = _InlineEdgar(tmp_path, unsafe=True)
    monkeypatch.setattr(
        inline_xbrl, "verify_manifest",
        lambda *args, **kwargs: pytest.fail("unsafe source must not parse"),
    )

    result = sync.retain_inline_statements(
        conn, {"0000000001"}, progress=lambda *args: None, edgar=edgar,
    )

    assert result["0000000001"]["state"] == "error"
    assert "unsafe SEC filing document name" in result["0000000001"]["error"]
    assert not (tmp_path / "escape.xml").exists()


def test_process_worker_uses_the_same_retained_supplement_loader(tmp_path, monkeypatch):
    cik = "0000000001"
    (tmp_path / f"companyfacts_{cik}.json").write_text(json.dumps({"facts": {}}))
    monkeypatch.setattr(
        inline_xbrl, "load_retained_supplement",
        lambda cache, requested, facts: ({"facts": {}, "supplement": requested}, "applied"),
    )
    monkeypatch.setattr(
        sync, "_derive_evidence",
        lambda bundle, assume_absent_zero=False: (
            "ok", {"supplement": bundle.facts["supplement"],
                   "state": bundle.supplement_state},
        ),
    )

    assert sync._derive_cached_worker((cik, "TEST", None, str(tmp_path))) == (
        cik, ("ok", {"supplement": cik, "state": "applied"}),
    )
