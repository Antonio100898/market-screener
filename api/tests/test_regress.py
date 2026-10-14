"""The real-company regression gate must include final UI calculations."""

from screener.regress import _flat, _fx_history_for_row, _price_history_for_row


def test_only_current_quote_fields_are_volatile_not_historical_multiples():
    row = {
        "price": 100.0,
        "price_asof": "2026-08-23T00:00:00+00:00",
        "market_cap": 1_000_000.0,
        "dividend_yield": 2.5,
        "annual_ratios": {"2025": {"price": 90.0, "pe": 12.0, "pb": 3.0}},
    }

    flat = _flat(row)

    assert "price" not in flat and "price_asof" not in flat
    assert flat["market_cap"] == 1_000_000.0
    assert flat["dividend_yield"] == 2.5
    assert flat["annual_ratios.2025.price"] == 90.0
    assert flat["annual_ratios.2025.pe"] == 12.0
    assert flat["annual_ratios.2025.pb"] == 3.0


def test_regression_prices_history_only_for_resolved_listings(monkeypatch):
    calls = []

    def fake_price_history(conn, cik):
        calls.append((conn, cik))
        return [{"date": "2025-12-31", "close": 10.0}]

    monkeypatch.setattr("screener.regress.store.price_history", fake_price_history)

    assert _price_history_for_row(object(), "0000000001", {"listed": None}) == []
    assert calls == []

    conn = object()
    assert _price_history_for_row(conn, "0000000002", {"listed": "y"}) == [
        {"date": "2025-12-31", "close": 10.0}
    ]
    assert calls == [(conn, "0000000002")]


def test_regression_reuses_export_fx_history_for_foreign_statements(monkeypatch):
    calls = []

    def fake_fx_history(conn, base, counter):
        calls.append((conn, base, counter))
        return {"base": base, "counter": counter,
                "closes": [("2025-12-31", 0.8)]}

    monkeypatch.setattr("screener.regress.store.fx_history", fake_fx_history)

    conn = object()
    assert _fx_history_for_row(conn, {"reporting_currency": "USD"}) is None
    assert calls == []
    assert _fx_history_for_row(conn, {"reporting_currency": "GBP"}) == {
        "base": "USD", "counter": "GBP", "closes": [("2025-12-31", 0.8)]}
    assert calls == [(conn, "USD", "GBP")]
