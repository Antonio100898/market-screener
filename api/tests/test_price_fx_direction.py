"""Directed valuation and separate USD size FX; no source/cache dependencies."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from screener import sync
from screener.models import Quote


def record(base="USD", counter="JPY", rate=150, closes=None):
    return dict(base=base, counter=counter, rate=rate,
        asof="2026-10-05T00:00:00+00:00", source="synthetic-directed-control",
        closes=closes if closes is not None else [(date(2025, 12, 19), rate)])


def priced_row(quote="USD", reporting="JPY"):
    return dict(cik="0000000001", ticker="CONTROL", listed="y",
        quote_currency=quote, reporting_currency=reporting, ttm_eps=300, tbvps=2000,
        ttm_eps_vintage={"2025-12-31": 300},
        annual_ratios={"2025": dict(end="2025-12-31", bvps=2000, tbvps=1800)},
        criteria=[dict(n=n, status="PASS", value=None, note=None) for n in (1, 2, 3, 4, 5, 7)])


def test_nonusd_valuation_record_does_not_replace_usd_size_record():
    row = priced_row("CAD", "JPY")
    sync._set_fx(row, record())
    sync._set_fx(row, record("CAD", "JPY", 108))
    assert row["fx"]["base"] == "USD"
    assert row["valuation_fx"]["base"] == "CAD"
    assert sync._valuation_price(row, 20) == Decimal("2160")
    assert sync.profiles._amount_in_usd(row, Decimal("150000000")) == Decimal("1000000")


def test_explicit_nonusd_quote_to_usd_is_retained():
    row = priced_row("EUR", "USD")
    sync._set_fx(row, record("EUR", "USD", 1.2))
    assert "fx" not in row
    assert sync._valuation_price(row, 100) == Decimal("120")


@pytest.mark.parametrize("base,counter,rate", [
    ("USD", "JPY", 150), ("JPY", "CAD", 1 / 108),
    ("CAD", "JPY", "Infinity"), ("CAD", "JPY", "NaN"),
    ("CAD", "JPY", 0), ("CAD", "JPY", -1)])
def test_wrong_or_invalid_historical_pair_withholds(base, counter, rate):
    row = priced_row("CAD", "JPY")
    sync._price_the_ratio_history(row, [(date(2025, 12, 20), 20)], record(base, counter, rate))
    assert "price" not in row["annual_ratios"]["2025"]


def test_fiscal_rates_use_exact_pair_and_no_future_rate():
    row = priced_row("CAD", "JPY")
    fx = record("CAD", "JPY", 110, [(date(2025, 12, 19), 108), (date(2026, 1, 1), 110)])
    sync._price_the_ratio_history(row, [(date(2025, 12, 20), 20)], fx)
    assert row["annual_ratios"]["2025"]["price"] == 2160
    assert row["annual_ratios"]["2025"]["fx_rate"] == 108
    row = priced_row("CAD", "JPY")
    sync._price_the_ratio_history(row, [(date(2025, 12, 20), 20)], record("CAD", "JPY", 110, [(date(2026, 1, 1), 110)]))
    assert "price" not in row["annual_ratios"]["2025"]


def test_native_history_does_not_need_or_consume_fx():
    row = priced_row("JPY", "JPY")
    sync._price_the_ratio_history(row, [(date(2025, 12, 20), 20)], record("USD", "JPY", "Infinity"))
    assert row["annual_ratios"]["2025"]["price"] == 20
    assert "fx_rate" not in row["annual_ratios"]["2025"]


@pytest.mark.parametrize("mode", ["full", "quote_only", "no_prices"])
def test_export_native_fx_absence_never_claims_valuation_withheld(tmp_path, monkeypatch, mode):
    row = priced_row("JPY", "JPY")
    requests = []
    stamp = datetime(2026, 10, 5, tzinfo=timezone.utc)
    quote = Quote(Decimal("20"), stamp, "synthetic-quote")
    history = SimpleNamespace(quote=quote, closes=((date(2025, 12, 20), Decimal("20")),), splits=())
    class Provider:
        def quote(self, ticker, **kwargs): return quote
        def history(self, ticker, **kwargs): return history
        def exchange_rate_history(self, base, counter):
            requests.append((base, counter))
            return None
        def close(self): pass
    class Connection:
        def commit(self): pass
    monkeypatch.setattr(sync, "DASHBOARD_JSON", tmp_path / "dashboard.json")
    monkeypatch.setattr(sync, "YahooPriceProvider", Provider)
    monkeypatch.setattr(sync.store, "needs_recompute", lambda *a, **k: [])
    monkeypatch.setattr(sync.store, "dashboard_rows", lambda c: [row])
    monkeypatch.setattr(sync.store, "fx_history", lambda *a: None)
    monkeypatch.setattr(sync.store, "price_history_record", lambda *a: (None, ()))
    monkeypatch.setattr(sync.store, "price_history", lambda *a: list(history.closes))
    monkeypatch.setattr(sync.store, "set_price_history", lambda *a: None)
    monkeypatch.setattr(sync.store, "set_state", lambda *a: None)
    monkeypatch.setattr(sync, "_mark_memberships", lambda *a: None)
    monkeypatch.setattr(sync, "_mark_peer_efficiency", lambda *a: None)
    monkeypatch.setattr(sync.profiles, "enrich", lambda row: {})
    sync.export(Connection(), with_prices=mode != "no_prices", quote_only=mode == "quote_only", progress=lambda *a: None)
    exported = json.loads(sync.DASHBOARD_JSON.read_text())["rows"][0]
    assert "valuation is withheld" not in (exported.get("fx_warning") or {}).get("note", "")
    if mode != "no_prices":
        assert "size comparison" in exported["fx_warning"]["note"]
        assert requests == [("USD", "JPY")]
        assert sync._valuation_price(exported, exported["price"]) == Decimal("20")


@pytest.mark.parametrize("rate", ["Infinity", "NaN", 0, -1, None, True, "1e1000", "1e-1000"])
def test_invalid_current_rate_does_not_become_any_fx_evidence(rate):
    row = priced_row("CAD", "JPY")
    sync._set_fx(row, record("CAD", "JPY", rate))
    assert "valuation_fx" not in row
    assert sync._valuation_price(row, 20) is None


def test_explicit_invalid_valuation_record_cannot_fall_back_to_usd_size_evidence():
    row = priced_row("USD", "JPY")
    row.update(fx=record(), valuation_fx=record("JPY", "USD", 1 / 150))
    assert sync._valuation_price(row, 20) is None


def test_reattachment_removes_stale_warning_and_keeps_two_purposes():
    row = priced_row("CAD", "JPY")
    sync._set_fx_records(row, {("USD", "JPY"): record()})
    assert "price-based valuation is withheld" in row["fx_warning"]["note"]
    assert "size comparison" not in row["fx_warning"]["note"]
    sync._set_fx_records(row, {("USD", "JPY"): record(), ("CAD", "JPY"): record("CAD", "JPY", 108)})
    assert "fx_warning" not in row
    assert row["fx"]["base"] == "USD" and row["valuation_fx"]["base"] == "CAD"
    sync._set_fx_records(row, {("CAD", "JPY"): record("CAD", "JPY", 108)})
    assert "size comparison" in row["fx_warning"]["note"]
    assert "valuation is withheld" not in row["fx_warning"]["note"]
    assert sync._valuation_price(row, 20) == Decimal("2160")


def test_regression_reads_actual_directed_pair_and_native_has_no_fx_lookup(monkeypatch):
    from screener import regress
    calls = []
    monkeypatch.setattr(sync.store, "fx_history", lambda conn, base, counter:
        calls.append((base, counter)) or record(base, counter, 108))
    assert regress._fx_history_for_row(None, priced_row("JPY", "JPY")) is None
    assert regress._fx_history_for_row(None, priced_row("CAD", "JPY"))["base"] == "CAD"
    assert regress._fx_history_for_row(None, priced_row("EUR", "USD"))["counter"] == "USD"
    assert calls == [("CAD", "JPY"), ("EUR", "USD")]


def test_audit_portfolio_and_capture_keep_the_valuation_pair_and_wrong_target_fails():
    from screener import audit, portfolio
    from screener.display_publication import capture_fields
    row = priced_row("CAD", "JPY")
    row.update(price=20, price_reporting_currency=2160)
    sync._set_fx(row, record())
    sync._set_fx(row, record("CAD", "JPY", 108))
    checks = {name: (shown, expected) for name, shown, expected, *_ in audit._identities(row)}
    assert checks["price_reporting_currency"] == (2160, 2160)
    row["price_reporting_currency"] = 3000
    checks = {name: (shown, expected) for name, shown, expected, *_ in audit._identities(row)}
    assert checks["price_reporting_currency"] == (3000, 2160)
    assert portfolio._price_on_reporting_basis(row, Decimal("10")) == Decimal("1080")
    captured = capture_fields("fx", row)
    assert captured["valuation_fx"] == row["valuation_fx"]
    assert captured["fx"] == row["fx"]
    assert capture_fields("fx", {}) == {}
    row["valuation_fx"] = record("USD", "JPY", 150)
    assert portfolio._price_on_reporting_basis(row, Decimal("10")) is None


@pytest.mark.parametrize("fx", [None, record("CAD", "EUR", 2), record("USD", "CAD", 2),
    record("EUR", "CAD", "Infinity"), record("EUR", "CAD", "NaN"),
    record("EUR", "CAD", 0), record("EUR", "CAD", -1)])
def test_audit_missing_bridge_never_certifies_stored_conversion_or_dependent_ratios(fx):
    from screener import audit
    row = dict(quote_currency="EUR", reporting_currency="CAD", price=40,
        price_reporting_currency=80, ttm_eps=4, tbvps=20, valuation_fx=fx,
        current_assets=100, current_liabilities=50,
        criteria=[dict(n=1, value=20), dict(n=7, value=4), dict(n=2, value=2)])
    checks = {name for name, *_ in audit._identities(row)}
    assert not {"price_reporting_currency", "pe", "ptbv"} & checks
    assert "current_ratio" in checks


def test_default_detail_uses_qualified_pairs_and_strict_does_not_rebuild(monkeypatch):
    from fastapi.testclient import TestClient
    from screener import api
    base = priced_row("CAD", "JPY")
    base.update(price=20, annual_ratios={}, currency="JPY")
    calls, reads = [], []
    class Cursor:
        def fetchone(self): return {"last_filing": None, "events_from": None}
        def __iter__(self): return iter(())
    class Connection:
        def execute(self, *args): return Cursor()
        def close(self): calls.append("closed")
    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    monkeypatch.setattr(api, "_dashboard_payload", lambda: {"rows": [base]})
    monkeypatch.setattr(api, "_latest_quarterly_filing", lambda cik: None)
    monkeypatch.setattr(api.store, "connect", Connection)
    monkeypatch.setattr(api.evidence, "EvidenceLoader", lambda *a: SimpleNamespace(load=lambda *a: None))
    monkeypatch.setattr(api.sync, "_derive_evidence", lambda *a, **k: calls.append("derive") or ("ok", dict(annual_ratios={})))
    monkeypatch.setattr(api.store, "price_history", lambda *a: [])
    monkeypatch.setattr(api.store, "fx_history", lambda conn, base, counter:
        reads.append((base, counter)) or record(base, counter, 150 if base == "USD" else 108))
    monkeypatch.setattr(api.profiles, "enrich", lambda row: {})
    client = TestClient(api.app)
    default = client.get("/company/CONTROL/dashboard")
    assert default.status_code == 200
    assert default.json()["price_reporting_currency"] == 2160
    assert default.json()["fx"]["base"] == "USD"
    assert default.json()["valuation_fx"]["base"] == "CAD"
    assert set(reads) == {("CAD", "JPY"), ("USD", "JPY")}
    calls.clear(); reads.clear()
    strict = client.get("/company/CONTROL/dashboard?assume_absent_zero=false")
    assert strict.status_code == 200
    assert calls == [] and reads == []


def test_anonymous_history_cannot_claim_a_directed_rate():
    row = priced_row("USD", "JPY")
    sync._price_the_ratio_history(row, [(date(2025, 12, 20), 20)], [(date(2025, 12, 19), 150)])
    assert "price" not in row["annual_ratios"]["2025"]


@pytest.mark.parametrize("mode", ["full", "quote_only", "no_prices"])
def test_export_acquires_or_reads_each_distinct_pair_once(tmp_path, monkeypatch, mode):
    rows = [priced_row("CAD", "JPY"), priced_row("CAD", "JPY"), priced_row("USD", "JPY")]
    for i, row in enumerate(rows):
        row.update(cik=str(i), ticker=f"CONTROL{i}")
    requests, writes, reads = [], [], []
    stamp = datetime(2026, 10, 5, tzinfo=timezone.utc)
    close = ((date(2025, 12, 20), Decimal("20")),)
    def history(price):
        return SimpleNamespace(quote=Quote(Decimal(str(price)), stamp, "synthetic-transport"),
            closes=((date(2025, 12, 19), Decimal(str(price))),), splits=())
    class Provider:
        def quote(self, ticker, **kwargs): return history(20).quote
        def history(self, ticker, **kwargs): return SimpleNamespace(quote=self.quote(ticker), closes=close, splits=())
        def exchange_rate_history(self, base, counter):
            requests.append((base, counter))
            return history(150 if base == "USD" else 108)
        def close(self): pass
    class Connection:
        def commit(self): pass
    monkeypatch.setattr(sync, "DASHBOARD_JSON", tmp_path / "dashboard.json")
    monkeypatch.setattr(sync, "YahooPriceProvider", Provider)
    monkeypatch.setattr(sync.store, "needs_recompute", lambda *a, **k: [])
    monkeypatch.setattr(sync.store, "dashboard_rows", lambda c: rows)
    monkeypatch.setattr(sync.store, "fx_history", lambda conn, base, counter:
        reads.append((base, counter)) or record(base, counter, 150 if base == "USD" else 108))
    monkeypatch.setattr(sync.store, "set_fx_history", lambda conn, base, counter, value: writes.append((base, counter)))
    monkeypatch.setattr(sync.store, "price_history_record", lambda *a: (None, ()))
    monkeypatch.setattr(sync.store, "price_history", lambda *a: close)
    monkeypatch.setattr(sync.store, "set_price_history", lambda *a: None)
    monkeypatch.setattr(sync.store, "set_state", lambda *a: None)
    monkeypatch.setattr(sync, "_mark_memberships", lambda *a: None)
    monkeypatch.setattr(sync, "_mark_peer_efficiency", lambda *a: None)
    monkeypatch.setattr(sync.profiles, "enrich", lambda row: {})
    sync.export(Connection(), with_prices=mode != "no_prices", quote_only=mode == "quote_only", progress=lambda *a: None)
    exported = json.loads(sync.DASHBOARD_JSON.read_text())["rows"]
    assert [row["annual_ratios"]["2025"]["price"] for row in exported] == [2160, 2160, 3000]
    if mode == "no_prices":
        assert set(reads) == {("USD", "JPY"), ("CAD", "JPY")} and len(reads) == 2
        assert requests == [] and writes == []
    else:
        assert set(requests) == {("USD", "JPY"), ("CAD", "JPY")} and len(requests) == 2
        assert set(writes) == set(requests) and len(writes) == 2
        assert reads == []
        assert all(row["fx"]["base"] == "USD" for row in exported)
        assert all(row["valuation_fx"]["base"] == "CAD" for row in exported[:2])
        assert "valuation_fx" not in exported[2]
