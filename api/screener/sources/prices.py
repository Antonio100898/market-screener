"""Layer 1: price quotes behind a swappable PriceProvider protocol."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Protocol

import httpx

from ..models import PriceHistory, Quote


class PriceProvider(Protocol):
    def quote(self, ticker: str, expected_currency: str = "USD") -> Quote | None: ...
    def history(self, ticker: str, expected_currency: str = "USD") -> PriceHistory | None: ...


YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
             "?range={range}&interval={interval}&includePrePost={include_pre_post}&events=splits")

_BAD = (httpx.HTTPError, InvalidOperation, KeyError, IndexError, TypeError, ValueError)


class YahooPriceProvider:
    # ponytail: free unofficial Yahoo endpoint; swap in a paid PriceProvider impl for production SLAs
    def __init__(self, timeout: float = 20.0, *, http: Any = None,
                 clock: Callable[[], datetime] | None = None):
        self._owns_http = http is None
        self._clock = clock
        self._http = http if http is not None else httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _chart(self, ticker: str, range_: str, interval: str,
               include_pre_post: bool = False) -> dict | None:
        # Alphabetic one-letter classes use a dash (BRK.B -> BRK-B). Numeric
        # Japanese listings keep their exchange suffix (6752.T).
        symbol = ticker.strip().upper()
        if re.fullmatch(r"[A-Z]+\.[A-Z]", symbol):
            symbol = symbol.replace(".", "-")
        try:
            resp = self._http.get(YAHOO_URL.format(
                symbol=symbol, range=range_, interval=interval,
                include_pre_post=str(include_pre_post).lower(),
            ))
            resp.raise_for_status()
            return resp.json()["chart"]["result"][0]
        except _BAD:
            return None  # unavailable is the protocol's signal -> criteria 1/7 go INSUFFICIENT

    @staticmethod
    def _periods(meta: dict) -> dict[str, tuple[int, int]]:
        out = {}
        for session, payload in (meta.get("currentTradingPeriod") or {}).items():
            try:
                start, end = int(payload["start"]), int(payload["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end > start:
                out[session.upper()] = (start, end)
        return out

    @staticmethod
    def _session_at(stamp: int, periods: dict[str, tuple[int, int]],
                    *, outside: str = "UNKNOWN") -> str:
        for session in ("PRE", "REGULAR", "POST"):
            bounds = periods.get(session)
            if bounds and bounds[0] <= stamp < bounds[1]:
                return session
        return outside

    @classmethod
    def _quote_from(cls, result: dict, *, now: datetime | None = None,
                    expected_currency: str = "USD") -> Quote | None:
        try:
            meta = result["meta"]
            price = Decimal(str(meta["regularMarketPrice"]))
            stamp = int(meta["regularMarketTime"])
            # Zero is the provider's missing/suspended sentinel, not a tradable
            # security price. A non-USD response means symbol identity did not
            # resolve to the US listing whose filings the row carries.
            if not price.is_finite() or price <= 0:
                return None
            if meta.get("currency") not in (None, expected_currency):
                return None
            session = "REGULAR"
            periods = cls._periods(meta)

            # Intraday charts include pre/post bars when requested. Scan from the
            # end because illiquid securities commonly have null intervals.
            stamps = result.get("timestamp") or []
            closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            for bar_stamp, close in reversed(list(zip(stamps, closes))):
                if close is None:
                    continue
                candidate = Decimal(str(close))
                bar_stamp = int(bar_stamp)
                if candidate.is_finite() and candidate > 0 and bar_stamp > stamp:
                    price, stamp = candidate, bar_stamp
                    session = cls._session_at(stamp, periods)
                break

            checked_at = now or datetime.now(tz=timezone.utc)
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            state = cls._session_at(
                int(checked_at.astimezone(timezone.utc).timestamp()), periods,
                outside="CLOSED" if periods else "UNKNOWN",
            )
            return Quote(
                price=price,
                asof=datetime.fromtimestamp(stamp, tz=timezone.utc),
                source="yahoo",
                session=session,
                market_state=state,
                market_timezone=meta.get("exchangeTimezoneName"),
                market_state_asof=checked_at.astimezone(timezone.utc),
            )
        except _BAD:
            return None

    def quote(self, ticker: str, expected_currency: str = "USD") -> Quote | None:
        result = self._chart(ticker, "1d", "5m", include_pre_post=True)
        return self._quote_from(result, now=self._clock() if self._clock else None,
                                expected_currency=expected_currency) if result else None

    def history(self, ticker: str, expected_currency: str = "USD") -> PriceHistory | None:
        """Five years of weekly closes, and the live quote that comes with them.

        Weekly history and an extended-hours quote use different intervals. The
        second, small intraday request is necessary because a weekly bar contains
        only regular-session closes.
        """
        result = self._chart(ticker, "5y", "1wk")
        if not result:
            return None
        quote_result = self._chart(ticker, "1d", "5m", include_pre_post=True)
        q = self._quote_from(quote_result or result, now=self._clock() if self._clock else None,
                             expected_currency=expected_currency)
        if q is None:
            return None
        try:
            stamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
        except _BAD:
            return PriceHistory(quote=q, closes=())
        series = []
        for t, c in zip(stamps, closes):
            if c is None:
                continue
            try:
                value = Decimal(str(c))
                if value.is_finite() and value > 0:
                    series.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), value))
            except _BAD:
                continue

        splits = []
        for event in (result.get("events", {}).get("splits", {}) or {}).values():
            try:
                numerator = Decimal(str(event["numerator"]))
                denominator = Decimal(str(event["denominator"]))
                stamp = event.get("date") or event.get("timestamp")
                factor = denominator / numerator
                if factor.is_finite() and factor > 0:
                    splits.append((datetime.fromtimestamp(stamp, tz=timezone.utc).date(), factor))
            except _BAD + (ZeroDivisionError,):
                continue
        return PriceHistory(quote=q, closes=tuple(series), splits=tuple(sorted(splits)))

    def exchange_rate_history(
        self, base_currency: str, counter_currency: str,
    ) -> PriceHistory | None:
        """Counter-currency units for one base unit, current and ten-year weekly.

        `USDJPY=X`, for example, is JPY per USD. The direction is part of the
        method contract so callers never infer whether a provider pair needs to
        be multiplied or divided. A direct pair is preferred; an available
        inverse pair is inverted explicitly and retains its symbol in provenance.
        """
        base = base_currency.strip().upper()
        counter = counter_currency.strip().upper()
        if not (re.fullmatch(r"[A-Z]{3}", base)
                and re.fullmatch(r"[A-Z]{3}", counter)):
            return None
        if base == counter:
            now = self._clock() if self._clock else datetime.now(tz=timezone.utc)
            return PriceHistory(
                quote=Quote(Decimal(1), now, "identity-fx"), closes=())

        def read(symbol: str, expected: str) -> PriceHistory | None:
            result = self._chart(symbol, "10y", "1wk")
            quote = self._quote_from(result, now=self._clock() if self._clock else None,
                                     expected_currency=expected) if result else None
            if result is None or quote is None:
                return None
            try:
                stamps = result.get("timestamp") or []
                closes = result["indicators"]["quote"][0]["close"]
            except _BAD:
                closes = []
                stamps = []
            series = []
            for stamp, close in zip(stamps, closes):
                if close is None:
                    continue
                try:
                    value = Decimal(str(close))
                    if value.is_finite() and value > 0:
                        series.append((
                            datetime.fromtimestamp(stamp, tz=timezone.utc).date(), value))
                except _BAD:
                    continue
            return PriceHistory(
                quote=replace(quote, source=f"yahoo-fx:{symbol}"),
                closes=tuple(series),
            )

        direct_symbol = f"{base}{counter}=X"
        direct = read(direct_symbol, counter)
        if direct is not None:
            return direct
        inverse_symbol = f"{counter}{base}=X"
        inverse = read(inverse_symbol, base)
        if inverse is None or inverse.quote.price <= 0:
            return None
        return PriceHistory(
            quote=replace(
                inverse.quote,
                price=Decimal(1) / inverse.quote.price,
                source=f"{inverse.quote.source}:inverted",
            ),
            closes=tuple((day, Decimal(1) / value)
                         for day, value in inverse.closes if value > 0),
        )
