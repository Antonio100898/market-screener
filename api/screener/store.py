"""Local store: what has been fetched, and what has been derived from it.

Raw companyfacts stay as files in the EdgarClient cache — they are large blobs
nothing queries. SQLite holds the small, queryable part: which companies exist,
when each last filed, and the derived snapshot per company.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import sectors
from .sources import cover

# Bump when normalisation changes meaning; snapshots below this are recomputed
# from stored raw facts, with no refetching.
ENGINE_VERSION = 181  # recognize repo debt, continuing-operations OCF, and oil/gas cash CapEx

DEFAULT_DB = Path.home() / ".cache" / "graham-screener" / "screener.db"
_WRITE_ATTEMPTS = 5   # a recompute must not fail because the site was being read

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    cik           TEXT PRIMARY KEY,
    ticker        TEXT,
    name          TEXT,
    last_filing   TEXT,   -- newest 10-K/10-Q filing date seen, drives resync
    facts_synced  TEXT,   -- when raw companyfacts were last fetched
    -- from SEC's submissions feed: authoritative, and the only sector source that
    -- survives 6,000 lookups (Yahoo rate-limits after one)
    sic           TEXT,
    industry      TEXT,   -- SEC's detailed SIC description
    sector        TEXT,   -- coarse, investor-facing grouping of the SIC code
    exchange      TEXT,
    filer_size    TEXT,
    first_filed   TEXT,   -- the company's first-ever SEC filing date; gates windowed tests
    events_from   TEXT,   -- oldest filing the event scan could see; the window it may claim
    incorporation TEXT,   -- SEC's state-or-country code; a digit in it means non-US
    listed        TEXT    -- 'y' from SEC, or 'external' for an explicitly imported primary listing
);
CREATE INDEX IF NOT EXISTS company_ticker ON company(ticker);
CREATE INDEX IF NOT EXISTS company_industry ON company(industry);

CREATE TABLE IF NOT EXISTS snapshot (
    cik            TEXT PRIMARY KEY,
    engine_version INTEGER NOT NULL,
    computed_at    TEXT NOT NULL,
    status         TEXT NOT NULL,   -- ok | pending_facts | foreign | no_xbrl | error
    data           TEXT             -- derived snapshot + screen result, JSON
);
CREATE INDEX IF NOT EXISTS snapshot_stale ON snapshot(engine_version);

-- keyed on CIK, not ticker: symbols change hands (VSCO became VSXY) but the
-- company identity does not
CREATE TABLE IF NOT EXISTS tracked (
    cik      TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    note     TEXT
);

-- Tracking is research intent; a portfolio is accounting history.  Keeping a
-- trade ledger instead of an `owned` flag preserves repeated buys, partial
-- sales, fees and the evidence that was visible when each decision was made.
CREATE TABLE IF NOT EXISTS portfolio (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_trade (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id      INTEGER NOT NULL,
    cik               TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity          TEXT NOT NULL,
    price             TEXT NOT NULL,
    fees              TEXT NOT NULL DEFAULT '0',
    currency          TEXT NOT NULL DEFAULT 'USD',
    executed_at       TEXT NOT NULL,
    broker            TEXT,
    account_label     TEXT,
    external_id       TEXT,
    note              TEXT,
    decision_snapshot TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolio(id),
    UNIQUE (portfolio_id, external_id)
);
CREATE INDEX IF NOT EXISTS portfolio_trade_portfolio
    ON portfolio_trade(portfolio_id, executed_at, id);
CREATE INDEX IF NOT EXISTS portfolio_trade_company
    ON portfolio_trade(portfolio_id, cik, executed_at, id);

-- Bonds and crypto are current holdings rather than SEC-backed securities. They
-- stay outside the immutable stock trade ledger; bond prices are manual and the
-- crypto price is the saved fallback for the live Coinbase quote.
CREATE TABLE IF NOT EXISTS portfolio_asset (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  INTEGER NOT NULL,
    asset_type    TEXT NOT NULL CHECK (asset_type IN ('BOND', 'CRYPTO')),
    symbol        TEXT,
    name          TEXT NOT NULL,
    quantity      TEXT NOT NULL,
    current_price TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    note          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
);
CREATE INDEX IF NOT EXISTS portfolio_asset_portfolio
    ON portfolio_asset(portfolio_id, asset_type, name, id);

-- No row means the owner has not supplied a cash balance.  It must not be
-- silently treated as zero in the total-allocation dashboard.
CREATE TABLE IF NOT EXISTS portfolio_cash (
    portfolio_id INTEGER PRIMARY KEY,
    amount       TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
);

-- weekly closes, kept so the price statistics can be recomputed without asking
-- the provider for five years of history again
CREATE TABLE IF NOT EXISTS price_history (
    cik        TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    series     TEXT NOT NULL   -- [[iso date, close], ...] oldest first
);

-- 8-K item codes: the events the filing index itself proves happened. The
-- document is never read, so only codes whose meaning is fixed by the form are
-- kept — an item number is evidence, a summary of the filing would be a guess.
CREATE TABLE IF NOT EXISTS filing_event (
    cik   TEXT NOT NULL,
    filed TEXT NOT NULL,   -- ISO date the 8-K was filed
    item  TEXT NOT NULL,   -- item number, e.g. "4.02"
    accn  TEXT NOT NULL,
    PRIMARY KEY (cik, accn, item)
);
CREATE INDEX IF NOT EXISTS filing_event_cik ON filing_event(cik);

-- What a filing's cover page says the ticker is. Company Facts cannot express it:
-- the elements are text and sit under a share-class axis, so both are stripped.
-- The ratio is the number that makes a depositary receipt's market cap right.
CREATE TABLE IF NOT EXISTS security_cover (
    cik    TEXT NOT NULL,
    symbol TEXT NOT NULL,
    accn   TEXT NOT NULL,   -- the filing the sentence was read from
    title  TEXT NOT NULL,   -- verbatim, so a reader can check the parse
    ratio  TEXT,            -- underlying shares per receipt; NULL when not a receipt
    read_at TEXT NOT NULL,
    PRIMARY KEY (cik, symbol)
);

-- One rate series is shared by every filer reporting in the same currency.
-- `base=USD, counter=JPY` means JPY per USD; fixing that direction in storage
-- prevents an accidental inversion from changing every valuation in a country.
CREATE TABLE IF NOT EXISTS fx_history (
    base        TEXT NOT NULL,
    counter     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    rate_asof   TEXT NOT NULL,
    rate        TEXT NOT NULL,
    source      TEXT NOT NULL,
    series      TEXT NOT NULL,
    PRIMARY KEY (base, counter)
);

-- Evidence can change without engine code changing: a newly parsed cover or a
-- newly published DERA quarter may settle a previously unknown share basis.
CREATE TABLE IF NOT EXISTS snapshot_dirty (
    cik        TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    marked_at  TEXT NOT NULL
);

-- A filing can be visible in EDGAR before its numeric facts reach Company Facts.
-- This queue retries that accession while the last complete, explicitly disclosed
-- snapshot remains available to the dashboard.
CREATE TABLE IF NOT EXISTS pending_filing (
    cik          TEXT PRIMARY KEY,
    accession    TEXT,
    filed        TEXT,
    reason       TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_checked TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


REQUIRED_TABLES = frozenset({"company", "snapshot", "sync_state", "tracked", "portfolio",
                             "portfolio_trade", "portfolio_asset", "portfolio_cash",
                             "price_history", "fx_history", "filing_event",
                             "security_cover", "snapshot_dirty", "pending_filing"})


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Opening a connection must never need a write lock, or the status endpoint
    fails while a sync job holds one. Schema is created only when absent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers see a consistent snapshot mid-write
    conn.execute("PRAGMA busy_timeout=10000")  # wait for a writer rather than erroring
    # Checking every table, not just the first: a later release adds tables, and an
    # existing database would otherwise never get them. The read stays lock-free;
    # only a genuinely missing table triggers a write.
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES <= have:
        conn.executescript(SCHEMA)
    return conn


def migrate(conn) -> None:
    """One-off repairs and column additions, run by sync jobs — never by a reader."""
    conn.executescript(SCHEMA)   # creates tables added after the first run
    have = {r["name"] for r in conn.execute("PRAGMA table_info(company)")}
    for col in ("sic", "industry", "sector", "exchange", "filer_size", "first_filed",
                "events_from", "incorporation", "listed"):
        if col not in have:
            conn.execute(f"ALTER TABLE company ADD COLUMN {col} TEXT")
    conn.execute("UPDATE company SET last_filing = NULL WHERE last_filing = ''")
    # a CIK is not a ticker: earlier loads wrote one when SEC's map had no symbol
    conn.execute("UPDATE company SET ticker = NULL "
                 "WHERE ticker GLOB '[0-9]*' AND length(ticker) = 10")
    backfill_sectors(conn)
    conn.commit()


def backfill_sectors(conn) -> None:
    """Sector is derived from SIC, so it can be filled in without refetching."""
    rows = conn.execute(
        "SELECT cik, sic FROM company WHERE sic IS NOT NULL AND sector IS NULL"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE company SET sector = ? WHERE cik = ?",
                     (sectors.sector_for(r["sic"]), r["cik"]))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_company(conn, cik: str, ticker: str | None, name: str | None,
                   last_filing: str | None = None, facts_synced: bool = False) -> None:
    conn.execute(
        """INSERT INTO company (cik, ticker, name, last_filing, facts_synced)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(cik) DO UPDATE SET
             ticker       = COALESCE(excluded.ticker, company.ticker),
             name         = COALESCE(excluded.name, company.name),
             -- NULLIF keeps "never seen filing" as NULL; an empty string would be
             -- NOT NULL and queue every known ticker for refetch
             last_filing  = NULLIF(MAX(COALESCE(excluded.last_filing, ''),
                                       COALESCE(company.last_filing, '')), ''),
             facts_synced = COALESCE(excluded.facts_synced, company.facts_synced)""",
        (cik, ticker, name, last_filing, _now() if facts_synced else None),
    )


def put_snapshot(conn, cik: str, status: str, data: dict | None) -> None:
    """A long recompute runs while the dashboard is being served, and the two
    share one database. WAL lets them, but a writer can still meet a moment when
    the file is briefly held; waiting a beat is the whole remedy, and failing the
    run would mean taking the site down to recompute it."""
    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return _put_snapshot(conn, cik, status, data)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _WRITE_ATTEMPTS - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


def _put_snapshot(conn, cik: str, status: str, data: dict | None) -> None:
    if status == "pending_facts":
        pending = (data or {}).get("data_pending") or {}
        now = _now()
        conn.execute(
            """INSERT INTO pending_filing
                   (cik, accession, filed, reason, first_seen, last_checked)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cik) DO UPDATE SET
                 accession = excluded.accession,
                 filed = excluded.filed,
                 reason = excluded.reason,
                 last_checked = excluded.last_checked""",
            (cik, pending.get("accession"), pending.get("filed"),
             pending.get("note") or "SEC structured facts pending", now, now),
        )
        if data and data.get("cik"):
            # `_derive` recomputed the last complete filing under the current
            # engine. It is an ordinary usable snapshot with an explicit freshness
            # warning, not stale arithmetic and not a fabricated current filing.
            status = "ok"
        else:
            existing = conn.execute(
                "SELECT status, data FROM snapshot WHERE cik = ?", (cik,)).fetchone()
            if existing and existing["status"] == "ok" and existing["data"]:
                preserved = json.loads(existing["data"])
                preserved["data_pending"] = pending
                conn.execute("UPDATE snapshot SET data = ? WHERE cik = ?",
                             (json.dumps(preserved), cik))
                return
    else:
        conn.execute("DELETE FROM pending_filing WHERE cik = ?", (cik,))
    conn.execute(
        """INSERT INTO snapshot (cik, engine_version, computed_at, status, data)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(cik) DO UPDATE SET
             engine_version = excluded.engine_version,
             computed_at    = excluded.computed_at,
             status         = excluded.status,
             data           = excluded.data""",
        (cik, ENGINE_VERSION, _now(), status, json.dumps(data) if data else None),
    )
    conn.execute("DELETE FROM snapshot_dirty WHERE cik = ?", (cik,))


# a filer with no XBRL on SEC's side has nothing a recompute could read — only a
# refetch can change it, so it never counts as "stale under the current engine"
_UNRECOMPUTABLE = "('no_xbrl')"

# SEC's ticker file suffixes a preferred series with -P and an optional series
# letter (OAK-PA, ETI-P). A share class is suffixed with the class letter alone
# (BRK-B, BF-B) and is genuinely the common, so the P is what distinguishes them.
_PREFERRED_TICKER = "(ticker GLOB '*-P' OR ticker GLOB '*-P[A-Z]')"


def needs_recompute(conn, *, eligible_only: bool = False) -> list[str]:
    """Companies whose derived snapshot predates the current engine — recomputed
    from stored raw facts, never refetched.

    Routine derive/export jobs need only securities that can enter the dashboard.
    Tickerless filers and preferred-only symbols remain safely stale until SEC's
    mapping makes them eligible; an exhaustive maintenance run can still request
    every cached snapshot with the default ``eligible_only=False``.
    """
    eligible = (" AND c.ticker IS NOT NULL AND NOT " + _PREFERRED_TICKER
                if eligible_only else "")
    company_join = " JOIN company c USING (cik)" if eligible_only else ""
    rows = conn.execute(
        f"""SELECT s.cik FROM snapshot s{company_join}
             WHERE s.engine_version < ? AND s.status NOT IN {_UNRECOMPUTABLE}{eligible}
           UNION
           SELECT d.cik FROM snapshot_dirty d JOIN snapshot s USING (cik){company_join}
             WHERE s.status NOT IN {_UNRECOMPUTABLE}{eligible}
           ORDER BY cik""",
        (ENGINE_VERSION,),
    ).fetchall()
    return [r["cik"] for r in rows]


def mark_snapshot_dirty(conn, cik: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO snapshot_dirty (cik, reason, marked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET reason = excluded.reason, marked_at = excluded.marked_at",
        (cik, reason, _now()),
    )


def needs_refetch(conn) -> list[str]:
    """Companies that filed something newer than our last fetch. A new filing can
    restate years we already hold, so 'do we have the latest period' is not enough."""
    rows = conn.execute(
        """SELECT cik FROM company
           WHERE (last_filing IS NOT NULL
                  AND (facts_synced IS NULL OR substr(facts_synced, 1, 10) < last_filing))
              OR EXISTS (SELECT 1 FROM pending_filing p WHERE p.cik = company.cik)"""
    ).fetchall()
    return [r["cik"] for r in rows]


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def dashboard_rows(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT c.cik, c.ticker, c.name, c.industry, c.sector, c.exchange, c.filer_size,
                  c.first_filed, c.events_from, c.last_filing, c.incorporation, c.listed,
                  s.data
           FROM snapshot s JOIN company c USING (cik)
           WHERE s.status = 'ok' AND s.data IS NOT NULL
             -- an unlisted filer has no ticker, no price, and cannot be bought
             AND c.ticker IS NOT NULL
             -- ...and a preferred series is not the common: its price belongs to a
             -- security with its own claim, while every figure derived here — the
             -- earnings, the book value, the share count — is the common's. OAK-PA
             -- was priced at a P/E of 8.06 on Oaktree's numbers.
             AND NOT """ + _PREFERRED_TICKER + """"""
    ).fetchall()
    events = events_by_cik(conn)
    out = []
    for r in rows:
        d = json.loads(r["data"])
        d["ticker"] = r["ticker"] or d.get("ticker")
        d.update(name=r["name"], industry=r["industry"], sector=r["sector"],
                 exchange=r["exchange"], filer_size=r["filer_size"],
                 first_filed=r["first_filed"], last_filing=r["last_filing"],
                 incorporation=r["incorporation"], listed=r["listed"],
                 # what the filing index proved, and how far back it could see:
                 # a company with no events and no scan are different answers
                 filing_events=events.get(r["cik"], []), events_from=r["events_from"])
        out.append(d)
    return out


def set_cover(conn, cik: str, securities: list[dict], accn: str) -> None:
    """Every registered class a filing's cover names, with the symbol attached."""
    securities = cover.unique_securities(securities)
    before = [tuple(r) for r in conn.execute(
        "SELECT symbol, accn, title, ratio FROM security_cover WHERE cik = ? ORDER BY symbol",
        (cik,),
    )]
    after = sorted((s["symbol"], accn, s["title"],
                    str(s["ratio"]) if s.get("ratio") is not None else None)
                   for s in securities)
    conn.execute("DELETE FROM security_cover WHERE cik = ?", (cik,))
    conn.executemany(
        """INSERT OR REPLACE INTO security_cover (cik, symbol, accn, title, ratio, read_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(cik, s["symbol"], accn, s["title"],
          str(s["ratio"]) if s.get("ratio") is not None else None, _now())
         for s in securities],
    )
    if before != after:
        mark_snapshot_dirty(conn, cik, "security cover changed")


def cover_for(conn, cik: str, ticker: str) -> dict | None:
    """The cover row for the security this ticker actually prices."""
    row = conn.execute(
        "SELECT symbol, accn, title, ratio FROM security_cover WHERE cik = ? AND symbol = ?",
        (cik, ticker),
    ).fetchone()
    return dict(row) if row else None


def covers_by_cik(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT cik, symbol, accn, title, ratio FROM security_cover"):
        out.setdefault(r["cik"], []).append(dict(r))
    return out


def resolve_ticker_conflicts(conn, mapping: dict[str, tuple[str, str]]) -> int:
    """One symbol, one company: whichever CIK SEC's map names today.

    `upsert_company` can only ever add a ticker — `COALESCE(excluded.ticker,
    company.ticker)` never clears one — so when a company reorganises and the
    symbol moves to a successor CIK, the predecessor keeps it forever and the
    dashboard shows the ticker twice. ATAI rendered at $1.77B and $2.72B at the
    same price, one row per entity, because the table keys on CIK while the price
    joins on ticker. Every case is a real succession: Gold Resource filed a Form 15
    while Goldgroup filed the 8-K12B that succeeded it.

    The predecessor keeps its facts, its snapshot and its history — it loses only
    the claim to a symbol somebody else now trades under, which is what makes it
    an unlisted filer and drops it out of the dashboard.
    """
    freed = conn.executemany(
        "UPDATE company SET ticker = NULL WHERE ticker = ? AND cik <> ?",
        [(ticker, cik) for cik, (ticker, _) in mapping.items() if ticker],
    ).rowcount
    # ...and record who the file still lists, which is a different question from
    # who still files. American Electric Power files 10-Qs and its submissions
    # index carries no ticker and no exchange at all; Farmer Brothers filed a Form
    # 15 in May. Both still show a price here, and a price for a security nobody
    # can buy is the one number this screen must never present as ordinary.
    conn.execute(
        "UPDATE company SET listed = NULL "
        "WHERE listed IS NOT NULL AND listed <> 'external'"
    )
    conn.executemany("UPDATE company SET listed = 'y' WHERE cik = ? AND ticker = ?",
                     [(cik, ticker) for cik, (ticker, _) in mapping.items() if ticker])
    return max(freed, 0)


def dashboard_ciks(conn) -> list[str]:
    """Just the identities behind the dashboard — for jobs that fetch per company
    and have no use for the snapshots themselves."""
    return [r["cik"] for r in conn.execute(
        """SELECT cik FROM snapshot JOIN company USING (cik)
           WHERE status = 'ok' AND data IS NOT NULL AND ticker IS NOT NULL
             AND NOT """ + _PREFERRED_TICKER)]


def set_first_filed(conn, cik: str, first_filed: str) -> None:
    conn.execute("UPDATE company SET first_filed = ? WHERE cik = ?", (first_filed, cik))


def set_incorporation(conn, cik: str, code: str | None, description: str | None) -> None:
    """Where the filer is incorporated, as SEC's own index states it. US states are
    two letters; every non-US jurisdiction carries a digit (E9 Cayman, X0 United
    Kingdom), which is the only reliable way this dataset can tell a foreign issuer
    from a domestic one."""
    if not code:
        return
    conn.execute("UPDATE company SET incorporation = ? WHERE cik = ?",
                 (f"{code}|{description or code}", cik))


def set_events(conn, cik: str, events: list[dict], scanned_from: str | None) -> None:
    """Replace this company's event record wholesale.

    A rescan reads one index that is itself the whole truth about the window it
    covers, so merging would keep events an amended index no longer shows — and
    would leave `events_from` describing a scan that no longer explains the rows.
    """
    conn.execute("DELETE FROM filing_event WHERE cik = ?", (cik,))
    conn.executemany(
        "INSERT OR IGNORE INTO filing_event (cik, filed, item, accn) VALUES (?, ?, ?, ?)",
        [(cik, e["filed"], e["item"], e["accn"]) for e in events],
    )
    conn.execute("UPDATE company SET events_from = ? WHERE cik = ?", (scanned_from, cik))


def events_by_cik(conn) -> dict[str, list[dict]]:
    """Every stored event, grouped — one query for the whole export."""
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT cik, filed, item, accn FROM filing_event ORDER BY cik, filed"
    ):
        out.setdefault(r["cik"], []).append(
            {"filed": r["filed"], "item": r["item"], "accn": r["accn"]})
    return out


def set_metadata(conn, cik: str, sic, industry, exchange, filer_size, ticker=None, name=None) -> None:
    conn.execute(
        """UPDATE company SET sic = COALESCE(?, sic), industry = COALESCE(?, industry),
               sector = COALESCE(?, sector),
               exchange = COALESCE(?, exchange), filer_size = COALESCE(?, filer_size),
               ticker = COALESCE(ticker, ?), name = COALESCE(?, name)
           WHERE cik = ?""",
        (sic, industry, sectors.sector_for(sic), exchange, filer_size, ticker, name, cik),
    )


def tracked(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT t.cik, t.added_at, t.note, c.ticker, c.name
           FROM tracked t LEFT JOIN company c USING (cik)
           ORDER BY t.added_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def track(conn, cik: str, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO tracked (cik, added_at, note) VALUES (?, ?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET note = COALESCE(excluded.note, tracked.note)",
        (cik, _now(), note),
    )
    conn.commit()


def untrack(conn, cik: str) -> bool:
    changed = conn.execute("DELETE FROM tracked WHERE cik = ?", (cik,)).rowcount
    conn.commit()
    return bool(changed)


def portfolios(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, base_currency, created_at FROM portfolio ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def create_portfolio(conn, name: str, base_currency: str = "USD") -> dict:
    created_at = _now()
    cursor = conn.execute(
        "INSERT INTO portfolio (name, base_currency, created_at) VALUES (?, ?, ?)",
        (name, base_currency, created_at),
    )
    conn.commit()
    return {
        "id": cursor.lastrowid,
        "name": name,
        "base_currency": base_currency,
        "created_at": created_at,
    }


def ensure_portfolio(conn, name: str = "Paper", base_currency: str = "USD") -> dict:
    row = conn.execute(
        "SELECT id, name, base_currency, created_at FROM portfolio WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if row is not None:
        return dict(row)
    return create_portfolio(conn, name, base_currency)


def portfolio_by_id(conn, portfolio_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, base_currency, created_at FROM portfolio WHERE id = ?",
        (portfolio_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def portfolio_trades(conn, portfolio_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, portfolio_id, cik, ticker, side, quantity, price, fees,
                  currency, executed_at, broker, account_label, external_id, note,
                  decision_snapshot, created_at
             FROM portfolio_trade
            WHERE portfolio_id = ?
            ORDER BY executed_at, id""",
        (portfolio_id,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["decision_snapshot"] = json.loads(item["decision_snapshot"])
        out.append(item)
    return out


def add_portfolio_trade(
    conn,
    *,
    portfolio_id: int,
    cik: str,
    ticker: str,
    side: str,
    quantity: str,
    price: str,
    fees: str,
    currency: str,
    executed_at: str,
    decision_snapshot: dict,
    broker: str | None = None,
    account_label: str | None = None,
    external_id: str | None = None,
    note: str | None = None,
) -> dict:
    created_at = _now()
    cursor = conn.execute(
        """INSERT INTO portfolio_trade
               (portfolio_id, cik, ticker, side, quantity, price, fees, currency,
                executed_at, broker, account_label, external_id, note,
                decision_snapshot, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            portfolio_id,
            cik,
            ticker,
            side,
            quantity,
            price,
            fees,
            currency,
            executed_at,
            broker,
            account_label,
            external_id,
            note,
            json.dumps(decision_snapshot, allow_nan=False, separators=(",", ":")),
            created_at,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM portfolio_trade WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    result = dict(row)
    result["decision_snapshot"] = json.loads(result["decision_snapshot"])
    return result


def delete_portfolio_trade(conn, portfolio_id: int, trade_id: int) -> bool:
    changed = conn.execute(
        "DELETE FROM portfolio_trade WHERE portfolio_id = ? AND id = ?",
        (portfolio_id, trade_id),
    ).rowcount
    conn.commit()
    return bool(changed)


def portfolio_assets(conn, portfolio_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, portfolio_id, asset_type, symbol, name, quantity,
                  current_price, currency, note, created_at, updated_at
             FROM portfolio_asset
            WHERE portfolio_id = ?
            ORDER BY asset_type, name COLLATE NOCASE, id""",
        (portfolio_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_portfolio_asset(
    conn,
    *,
    portfolio_id: int,
    asset_type: str,
    symbol: str | None,
    name: str,
    quantity: str,
    current_price: str,
    currency: str,
    note: str | None = None,
) -> dict:
    now = _now()
    cursor = conn.execute(
        """INSERT INTO portfolio_asset
               (portfolio_id, asset_type, symbol, name, quantity, current_price,
                currency, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (portfolio_id, asset_type, symbol, name, quantity, current_price,
         currency, note, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM portfolio_asset WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def update_portfolio_asset(
    conn,
    *,
    portfolio_id: int,
    asset_id: int,
    asset_type: str,
    symbol: str | None,
    name: str,
    quantity: str,
    current_price: str,
    currency: str,
    note: str | None = None,
) -> dict | None:
    now = _now()
    changed = conn.execute(
        """UPDATE portfolio_asset
              SET asset_type = ?, symbol = ?, name = ?, quantity = ?,
                  current_price = ?, currency = ?, note = ?, updated_at = ?
            WHERE portfolio_id = ? AND id = ?""",
        (asset_type, symbol, name, quantity, current_price, currency, note, now,
         portfolio_id, asset_id),
    ).rowcount
    conn.commit()
    if not changed:
        return None
    row = conn.execute(
        "SELECT * FROM portfolio_asset WHERE id = ?", (asset_id,)
    ).fetchone()
    return dict(row)


def delete_portfolio_asset(conn, portfolio_id: int, asset_id: int) -> bool:
    changed = conn.execute(
        "DELETE FROM portfolio_asset WHERE portfolio_id = ? AND id = ?",
        (portfolio_id, asset_id),
    ).rowcount
    conn.commit()
    return bool(changed)


def portfolio_cash(conn, portfolio_id: int) -> dict | None:
    row = conn.execute(
        "SELECT amount, updated_at FROM portfolio_cash WHERE portfolio_id = ?",
        (portfolio_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def set_portfolio_cash(conn, portfolio_id: int, amount: str) -> dict:
    updated_at = _now()
    conn.execute(
        """INSERT INTO portfolio_cash (portfolio_id, amount, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(portfolio_id) DO UPDATE SET
             amount = excluded.amount,
             updated_at = excluded.updated_at""",
        (portfolio_id, amount, updated_at),
    )
    conn.commit()
    return {"amount": amount, "updated_at": updated_at}


def set_price_history(conn, cik: str, closes) -> None:
    """Closes as (date, Decimal) pairs; stored as floats — this is a chart series,
    not money being added up, and the statistics over it are ratios."""
    conn.execute(
        "INSERT INTO price_history (cik, fetched_at, series) VALUES (?, ?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET fetched_at = excluded.fetched_at, "
        "series = excluded.series",
        (cik, _now(), json.dumps([[d.isoformat(), float(c)] for d, c in closes])),
    )


def price_history(conn, cik: str) -> list[tuple[date, float]]:
    row = conn.execute("SELECT series FROM price_history WHERE cik = ?", (cik,)).fetchone()
    if row is None:
        return []
    return [(date.fromisoformat(d), c) for d, c in json.loads(row["series"])]


def price_history_record(conn, cik: str) -> tuple[datetime | None, list[tuple[date, float]]]:
    """Stored history with the fetch time needed to validate later split revisions."""
    row = conn.execute(
        "SELECT fetched_at, series FROM price_history WHERE cik = ?", (cik,)).fetchone()
    if row is None:
        return None, []
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except (TypeError, ValueError):
        fetched = None
    return fetched, [(date.fromisoformat(d), c) for d, c in json.loads(row["series"])]


def set_fx_history(conn, base: str, counter: str, history) -> None:
    """Persist one provider rate and its weekly history in a fixed direction."""
    conn.execute(
        """INSERT INTO fx_history
               (base, counter, fetched_at, rate_asof, rate, source, series)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(base, counter) DO UPDATE SET
             fetched_at = excluded.fetched_at,
             rate_asof = excluded.rate_asof,
             rate = excluded.rate,
             source = excluded.source,
             series = excluded.series""",
        (base.upper(), counter.upper(), _now(), history.quote.asof.isoformat(),
         str(history.quote.price), history.quote.source,
         json.dumps([[day.isoformat(), float(value)]
                     for day, value in history.closes])),
    )


def fx_history(conn, base: str, counter: str) -> dict | None:
    row = conn.execute(
        """SELECT base, counter, fetched_at, rate_asof, rate, source, series
           FROM fx_history WHERE base = ? AND counter = ?""",
        (base.upper(), counter.upper()),
    ).fetchone()
    if row is None:
        return None
    return {
        "base": row["base"],
        "counter": row["counter"],
        "fetched_at": row["fetched_at"],
        "asof": row["rate_asof"],
        "rate": float(row["rate"]),
        "source": row["source"],
        "closes": [(date.fromisoformat(day), value)
                   for day, value in json.loads(row["series"])],
    }


def stats(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    all_stale = len(needs_recompute(conn))
    eligible_stale = len(needs_recompute(conn, eligible_only=True))
    return {
        "companies": q("SELECT COUNT(*) FROM company"),
        "snapshots": q("SELECT COUNT(*) FROM snapshot"),
        "ok": q("SELECT COUNT(*) FROM snapshot WHERE status='ok'"),
        # "stale" is actionable work for the next routine derive/export.  Cached
        # tickerless/preferred-only rows are reported separately rather than
        # making an otherwise current dashboard look perpetually unfinished.
        "stale": eligible_stale,
        "deferred_stale": all_stale - eligible_stale,
        "pending_refetch": q(
            """SELECT COUNT(*) FROM company WHERE
               (last_filing IS NOT NULL
                AND (facts_synced IS NULL OR substr(facts_synced,1,10) < last_filing))
               OR EXISTS (SELECT 1 FROM pending_filing p WHERE p.cik = company.cik)"""),
        "last_daily_index": get_state(conn, "last_daily_index"),
        "price_histories": q("SELECT COUNT(*) FROM price_history"),
        "fx_histories": q("SELECT COUNT(*) FROM fx_history"),
        "companies_scanned_for_events": q(
            "SELECT COUNT(*) FROM company WHERE events_from IS NOT NULL"),
        "filing_events": q("SELECT COUNT(*) FROM filing_event"),
        "engine_version": ENGINE_VERSION,
        # freshness for the UI: when filings were last fetched, when the newest
        # snapshot was computed, when prices/dashboard were last rebuilt
        "last_fetch": q("SELECT MAX(facts_synced) FROM company"),
        "computed_at": q("SELECT MAX(computed_at) FROM snapshot"),
        "last_export": get_state(conn, "last_export"),
        "last_quote_refresh": get_state(conn, "last_quote_refresh"),
        "last_quote_refresh_updated": get_state(conn, "last_quote_refresh_updated"),
        "last_quote_refresh_failed": get_state(conn, "last_quote_refresh_failed"),
    }


def today() -> date:
    return datetime.now(timezone.utc).date()
