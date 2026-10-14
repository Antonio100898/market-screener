"""SQL reads of sealed display releases, without source or financial computation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import date

from sqlalchemy import and_, func, literal, or_, select, text
from sqlalchemy.engine import Engine

from .company_projection import PROJECTION_REVISION
from .company_query_schema import release_company_projection as p
from .postgres import (active_data_release, company_snapshot, data_release, release_company,
                       release_company_lineage, source_validity_generation, exact_figure_bundle,
                       figure_activation_transition, figure_dependency, figure_observation,
                       release_company_source, source_observation, source_check_occurrence)
from .source_validity import SourceValidityRepository
from .figure_history import (FigureHistoryRepository, FigureHistoryConflict, Period,
    history_name_digest, history_selection_record, history_observation_record, public_history_record)


class InvalidQuery(ValueError):
    pass


class QueryUnavailable(RuntimeError):
    pass


class RestartQuery(ValueError):
    pass


class HistoryNotFound(ValueError):
    pass


@dataclass(frozen=True)
class HistoryQuery:
    metric_path: str
    period_kind: str
    period_start: date | None = None
    period_end: date | None = None
    view: str = "STRICT"
    limit: int = 300

    def __post_init__(self):
        if not isinstance(self.metric_path, str) or not self.metric_path.strip():
            raise InvalidQuery("metric_path is required")
        if self.view not in {"STRICT", "ASSUMED", "CONSERVATIVE"}:
            raise InvalidQuery("unsupported history view")
        if type(self.limit) is not int or not 1 <= self.limit <= 300:
            raise InvalidQuery("history limit must be1–300")
        try:
            Period(kind=self.period_kind, start=self.period_start, end=self.period_end,
                   origin="UNKNOWN" if self.period_kind == "UNKNOWN" else "REPORTED")
        except ValueError as exc:
            raise InvalidQuery("invalid actual history period") from exc

    def selector(self):
        return dict(metric_path=self.metric_path, view=self.view,
            period=dict(kind=self.period_kind, start=self.period_start, end=self.period_end))

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.selector(), sort_keys=True, default=str,
            separators=(",", ":")).encode()).hexdigest()


SORT_COLUMNS = {"ticker": "ticker", "name": "name", "sector": "sector",
                "eps10": "positive_eps_years", "returns": "return_quality",
                "mcap": "market_cap", "price": "price", "offhigh": "off_high",
                "pe": "pe", "pe3": "pe3", "dividend": "dividend_yield", "n_pass": "criteria_passed"}
_BEST_FIRST = {"eps10", "returns", "mcap", "offhigh", "dividend", "n_pass"}
_STOPWORDS = {"the", "and", "for", "with", "not", "are", "was", "its", "inc", "corp"}
MAX_CURSOR_LENGTH = 8192


@dataclass(frozen=True)
class CompanyQuery:
    q: str = ""
    lens: str = "BOTH"
    fit: str = "ALL"
    gaps: str = "ALL"
    profiles: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()
    min_cap: float = 0
    min_met: int = 0
    min_positive_eps: int = 0
    max_pe: float = 0
    max_pe3: float = 0
    hide_na: bool = False
    hide_no_apply: bool = False
    below_ncav: bool = False
    sorts: tuple[tuple[str, int], ...] = (("ticker", 1),)
    limit: int = 300
    row_fields: str = "full"
    tracked_only: bool = False
    workspace_revision: int | None = None

    def __post_init__(self):
        if self.tracked_only or self.workspace_revision is not None:
            raise InvalidQuery("private query filters require authenticated workspace support")
        if self.row_fields not in {"full", "identity"}:
            raise InvalidQuery("unsupported row fields")
        if not isinstance(self.q, str) or len(self.q) > 1000:
            raise InvalidQuery("search must contain at most 1000 characters")
        for value, allowed in ((self.lens, {"BOTH", "ENTERPRISING", "DEFENSIVE"}),
                               (self.fit, {"ALL", "ALIGNED", "EVIDENCE_INCOMPLETE", "BLOCKED"}),
                               (self.gaps, {"ALL", "ALL_PASS", "ONE_GAP", "TWO_GAPS", "THREE_PLUS", "INCOMPLETE"})):
            if value not in allowed:
                raise InvalidQuery("unsupported query enum")
        for values in (self.profiles, self.sectors, self.venues, self.indexes):
            if len(values) > 100 or any(not isinstance(value, str) or not value or len(value) > 200 for value in values):
                raise InvalidQuery("invalid selection values")
        for value in (self.min_cap, self.max_pe, self.max_pe3):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise InvalidQuery("numeric bounds must be finite and nonnegative")
        for value, ceiling in ((self.min_met, 6), (self.min_positive_eps, 10), (self.limit, 300)):
            if type(value) is not int or not 0 <= value <= ceiling:
                raise InvalidQuery("invalid count or page limit")
        if not self.limit or len(self.sorts) > len(SORT_COLUMNS) + 1:
            raise InvalidQuery("invalid page limit or sort count")
        if any(key not in SORT_COLUMNS and key != "fit" or type(direction) is not int or direction not in (-1, 1)
               for key, direction in self.sorts):
            raise InvalidQuery("unsupported sort")
        if len({key for key, _ in self.sorts}) != len(self.sorts):
            raise InvalidQuery("duplicate sort key")

    def fingerprint(self):
        values = asdict(self)
        values["q"] = self.q.strip().lower()
        for field in ("profiles", "sectors", "venues", "indexes"):
            values[field] = sorted(set(values[field]))
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _search(q):
    phrase = q.strip().lower()
    if not phrase:
        return literal(True)
    words = [word for word in re.split(r"[\s,]+", phrase) if len(word) > 2 and word not in _STOPWORDS]
    return or_(*(func.strpos(p.c.search_hay, needle) > 0 for needle in [phrase, *words]))


def _filters(query):
    conditions = [_search(query.q)]
    for values, column in ((query.profiles, p.c.profile), (query.sectors, func.coalesce(p.c.sector, "Unclassified").collate("C")),
                           (query.venues, p.c.exchange)):
        if values:
            conditions.append(column.in_(values))
    if query.indexes:
        named = [name for name in query.indexes if name != "Outside"]
        conditions.append(or_(p.c.index_memberships.overlap(named),
                              func.cardinality(p.c.index_memberships) == 0 if "Outside" in query.indexes else literal(False)))
    lens = p.c.enterprising_verdict if query.lens == "ENTERPRISING" else p.c.defensive_verdict
    if query.lens != "BOTH":
        conditions.append(lens.is_distinct_from("OUT_OF_SCOPE"))
    if query.fit != "ALL":
        conditions.append(or_(p.c.enterprising_verdict == query.fit, p.c.defensive_verdict == query.fit)
                          if query.lens == "BOTH" else lens == query.fit)
    if query.lens == "ENTERPRISING" and query.gaps != "ALL":
        conditions.append(p.c.enterprising_gap == query.gaps)
    for bound, column in ((query.min_cap, p.c.market_cap), (query.min_met, p.c.criteria_passed),
                          (query.min_positive_eps, p.c.positive_eps_years)):
        if bound:
            conditions.append(column >= bound)
    for bound, column in ((query.max_pe, p.c.pe), (query.max_pe3, p.c.pe3)):
        if bound:
            conditions.extend((column > 0, column <= bound))
    for enabled, condition in ((query.hide_na, ~p.c.unjudged), (query.hide_no_apply, ~p.c.inapplicable),
                               (query.below_ncav, p.c.net_net)):
        if enabled:
            conditions.append(condition)
    return conditions


def _sorts(query):
    sorts = [(SORT_COLUMNS[key], -direction if key in _BEST_FIRST else direction)
             for key, direction in query.sorts if key != "fit"]
    if not any(name == "ticker" for name, _ in sorts):
        sorts.append(("ticker", 1))
    return [*sorts, ("security_id", 1)]


def _seek(sorts, values):
    branches, prefix = [], []
    for (name, direction), value in zip(sorts, values):
        column = p.c[name]
        if value is not None:
            after = column > value if direction == 1 else column < value
            branches.append(and_(*prefix, or_(after, column.is_(None))))
        prefix.append(column.is_not_distinct_from(value))
    return or_(*branches) if branches else literal(False)


class CursorCodec:
    def __init__(self, secret: bytes | None = None):
        self.secret = secret if secret is not None else os.environ.get("SCREENER_CURSOR_SECRET", "").encode()

    def require_ready(self):
        if len(self.secret) < 32:
            raise QueryUnavailable("configure a stable SCREENER_CURSOR_SECRET of at least 32 bytes")

    def encode(self, payload):
        self.require_ready()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        signature = hmac.digest(self.secret, raw, "sha256")
        token = base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")
        if len(token) > MAX_CURSOR_LENGTH:
            raise InvalidQuery("cursor exceeds token limit")
        return token

    def decode(self, token):
        self.require_ready()
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_CURSOR_LENGTH:
            raise InvalidQuery("invalid cursor")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.secret, payload, "sha256")):
                raise ValueError("signature")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ValueError("object")
            return result
        except (ValueError, UnicodeError, TypeError) as exc:
            raise InvalidQuery("invalid cursor") from exc


class CompanyQueryRepository:
    def __init__(self, engine: Engine, *, cursor_secret: bytes | None = None):
        self.engine = engine
        self.cursors = CursorCodec(cursor_secret)

    @contextmanager
    def _read_snapshot(self):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                yield connection

    @staticmethod
    def _generation(connection):
        generation = connection.scalar(select(source_validity_generation.c.generation).where(
            source_validity_generation.c.singleton_id == 1))
        if generation is None:
            raise QueryUnavailable("source validity generation is unavailable")
        return generation

    @staticmethod
    def _assessment(connection, release_id, security_ids, generation):
        if security_ids:
            if len(security_ids) > 300:
                raise InvalidQuery("source assessment page exceeds 300 securities")
            result = SourceValidityRepository.assess_on_connection(connection, release_id, security_ids)
        else:
            result = {"release_id": release_id, "validity_generation": generation, "companies": [],
                      "freshness_target": None, "numeric_invalidation_assessed": False}
        return {**result, "assessment_scope": "CAPTURED_DEPENDENCIES_ONLY", "newest_filings_assessed": False,
                "official_financial_readiness": "NOT_ASSESSED"}

    @staticmethod
    def _release(connection, release_id):
        if release_id is not None and (type(release_id) is not int or release_id <= 0):
            raise InvalidQuery("release must be a positive integer")
        statement = select(data_release).where(data_release.c.published_at.is_not(None))
        if release_id is None:
            statement = statement.join(active_data_release, active_data_release.c.release_id == data_release.c.release_id)
        else:
            statement = statement.where(data_release.c.release_id == release_id)
        release = connection.execute(statement).mappings().one_or_none()
        if release is None:
            if release_id is not None:
                raise RestartQuery("release unavailable; restart the query at the current release")
            raise QueryUnavailable("no published company release")
        selected = release["release_id"]
        if release["projection_revision"] != PROJECTION_REVISION or release["input_bundle_sha256"] is None:
            raise QueryUnavailable("published release has no supported display manifest")
        members = connection.scalar(select(func.count()).select_from(release_company).where(release_company.c.release_id == selected))
        coverage = connection.execute(select(func.count(), func.min(p.c.projection_revision), func.max(p.c.projection_revision)).where(
            p.c.release_id == selected)).one()
        if not members or coverage != (members, PROJECTION_REVISION, PROJECTION_REVISION):
            raise QueryUnavailable("published release has no complete supported display projection")
        engines = list(connection.scalars(select(company_snapshot.c.engine_revision).join(
            release_company, release_company.c.snapshot_id == company_snapshot.c.snapshot_id).where(
            release_company.c.release_id == selected).distinct().order_by(company_snapshot.c.engine_revision)))
        return {"release_id": selected, "published_at": release["published_at"],
                "projection_revision": PROJECTION_REVISION, "engine_revisions": engines, "total": members,
                "input_bundle_sha256": release["input_bundle_sha256"]}

    def list(self, query: CompanyQuery, *, release_id: int | None = None, cursor: str | None = None):
        self.cursors.require_ready()
        decoded = self.cursors.decode(cursor) if cursor else None
        sorts = _sorts(query)
        if decoded is not None:
            if decoded.get("contract") != 1 or decoded.get("projection") != PROJECTION_REVISION or decoded.get("query") != query.fingerprint():
                raise InvalidQuery("cursor belongs to another query or contract")
            pinned = decoded.get("release")
            if type(pinned) is not int or pinned <= 0 or release_id is not None and pinned != release_id:
                raise InvalidQuery("cursor belongs to another release")
            release_id = pinned
            anchor_id = decoded.get("anchor")
            if type(anchor_id) is not int or not 0 < anchor_id <= 2**63 - 1:
                raise InvalidQuery("invalid cursor security")
            cursor_generation = decoded.get("validity_generation")
            if type(cursor_generation) is not int or cursor_generation < 0:
                raise RestartQuery("cursor has no supported source validity identity; restart the query")
        with self._read_snapshot() as connection:
            release = self._release(connection, release_id)
            generation = self._generation(connection)
            # ponytail: global semantic generation may restart unrelated queries; use release-scoped generations if measured churn requires it.
            if decoded is not None and cursor_generation != generation:
                raise RestartQuery("accepted source state changed; restart the query")
            base = [p.c.release_id == release["release_id"], *_filters(query)]
            counts = self._counts(connection, base)
            matches = connection.scalar(select(func.count()).select_from(p).where(
                p.c.release_id == release["release_id"], _search(query.q))) if query.q.strip() else 0
            row_columns = [p.c.security_id, p.c.ticker, p.c.name] if query.row_fields == "identity" else [p.c.security_id, p.c.enriched_payload]
            statement = select(*row_columns).where(*base)
            if decoded is not None:
                anchor = connection.execute(select(*(p.c[name] for name, _ in sorts)).where(
                    *base, p.c.security_id == anchor_id)).one_or_none()
                if anchor is None:
                    raise InvalidQuery("cursor security is absent from its release query")
                statement = statement.where(_seek(sorts, anchor))
            statement = statement.order_by(*(p.c[name].asc().nulls_last() if direction == 1
                                            else p.c[name].desc().nulls_last() for name, direction in sorts)).limit(query.limit + 1)
            page = connection.execute(statement).mappings().all()
            more = len(page) > query.limit
            page = page[:query.limit]
            next_cursor = self.cursors.encode({"contract": 1, "projection": PROJECTION_REVISION,
                                              "release": release["release_id"], "query": query.fingerprint(),
                                              "anchor": page[-1]["security_id"], "validity_generation": generation}) if more else None
            rows = [{"security_id": row["security_id"], "ticker": row["ticker"], "name": row["name"]} for row in page] if query.row_fields == "identity" else [
                {**row["enriched_payload"], "security_id": row["security_id"]} for row in page]
            return {"release": release, "rows": rows,
                    "matched": counts["matched"], "counts": counts, "matches_anywhere": matches,
                    "next_cursor": next_cursor,
                    "source_validity": self._assessment(connection, release["release_id"],
                                                        [row["security_id"] for row in page], generation)}

    @staticmethod
    def _counts(connection, base):
        columns = [func.count().label("matched")]
        columns.extend(func.count().filter(p.c[field]).label(field) for field in ("unjudged", "inapplicable", "net_net"))
        columns.extend(func.count().filter(p.c.positive_eps_years >= floor).label(f"eps_{floor}") for floor in (5, 6, 7, 9, 10))
        columns.extend(func.count().filter(p.c.criteria_passed >= floor).label(f"met_{floor}") for floor in range(1, 7))
        result = dict(connection.execute(select(*columns).where(*base)).mappings().one())
        result["judged"] = result["matched"] - result["unjudged"]
        return result

    def facets(self, *, release_id: int | None = None):
        with self._read_snapshot() as connection:
            release = self._release(connection, release_id)
            base = p.c.release_id == release["release_id"]
            result = {"release": release, "validity_generation": self._generation(connection)}
            for key, column in (("sectors", func.coalesce(p.c.sector, "Unclassified").collate("C")), ("venues", p.c.exchange), ("profiles", p.c.profile)):
                statement = select(column.label("name"), func.count().label("count"), func.min(p.c.universe_ordinal).label("first")).where(base)
                if key == "venues":
                    statement = statement.where(p.c.exchange.is_not(None), p.c.exchange != "")
                statement = statement.group_by(column)
                if key == "profiles":
                    from .company_query_schema import QUERY_COLLATION
                    statement = statement.order_by(column.collate(QUERY_COLLATION))
                else:
                    statement = statement.order_by(func.count().desc(), func.min(p.c.universe_ordinal))
                result[key] = [[row.name, row.count] for row in connection.execute(statement)]
            membership = func.unnest(p.c.index_memberships).table_valued("name", with_ordinality="position").render_derived().lateral()
            expanded = select(membership.c.name, p.c.universe_ordinal, membership.c.position,
                              func.count().over(partition_by=membership.c.name).label("count"),
                              func.row_number().over(partition_by=membership.c.name, order_by=(p.c.universe_ordinal, membership.c.position)).label("rank")
                              ).select_from(p.join(membership, literal(True))).where(base).subquery()
            indexes = [dict(row) for row in connection.execute(select(expanded).where(expanded.c.rank == 1)).mappings()]
            outside = connection.scalar(select(func.count()).select_from(p).where(base, func.cardinality(p.c.index_memberships) == 0))
            indexes.append({"name": "Outside", "count": outside, "universe_ordinal": -1, "position": 0})
            rank = {"DJIA": 0, "S&P 500": 1, "Nasdaq 100": 2, "Nasdaq Comp": 3}
            indexes.sort(key=lambda row: (rank.get(row["name"], 9), row["universe_ordinal"], row["position"]))
            result["indexes"] = [[row["name"], row["count"]] for row in indexes]
            return result

    def overview(self, *, release_id: int | None = None):
        with self._read_snapshot() as connection:
            release = self._release(connection, release_id)
            base = [p.c.release_id == release["release_id"], p.c.index_memberships.contains(["S&P 500"])]
            result = {"release": release, "validity_generation": self._generation(connection),
                      "members": connection.scalar(select(func.count()).select_from(p).where(*base))}
            for key in ("pe", "pe3"):
                column = p.c[key]
                median, count = connection.execute(select(func.percentile_cont(0.5).within_group(column), func.count()).where(*base, column > 0)).one()
                result[key] = {"median": median, "count": count}
            return result

    def detail(self, *, security_id: int | None = None, ticker: str | None = None,
               release_id: int | None = None, assume_absent_zero: bool = True):
        if (security_id is None) == (ticker is None):
            raise InvalidQuery("select exactly one security or ticker")
        if security_id is not None and (type(security_id) is not int or security_id <= 0):
            raise InvalidQuery("security must be a positive integer")
        if ticker is not None and (not isinstance(ticker, str) or not ticker.strip() or len(ticker) > 100):
            raise InvalidQuery("invalid ticker")
        with self._read_snapshot() as connection:
            release = self._release(connection, release_id)
            generation = self._generation(connection)
            identity = p.c.security_id == security_id if security_id is not None else p.c.ticker.collate("C") == ticker.strip().upper()
            row = connection.execute(select(p).where(p.c.release_id == release["release_id"], identity)).mappings().one_or_none()
            if row is None:
                return None
            payload = row["assumed_payload"] if assume_absent_zero and row["assumption_status"] == "APPLIED" else row["enriched_payload"]
            payload = dict(payload)
            if assume_absent_zero and row["assumption_status"] == "UNAVAILABLE":
                payload["assumption_mode"] = {"requested": True, "status": "UNAVAILABLE", "applied": [], "details": [],
                                              "note": "No validated assumed view exists for this release; strict filing values are shown."}
            return {"release": release, "security_id": row["security_id"], "row": payload,
                    "input_bundle_sha256": row["input_bundle_sha256"],
                    "source_validity": self._assessment(connection, release["release_id"], [row["security_id"]], generation)}

    def status(self):
        with self._read_snapshot() as connection:
            active = connection.scalar(select(active_data_release.c.release_id))
            generation = self._generation(connection)
            lineage = None
            if active is not None:
                members = connection.scalar(select(func.count()).select_from(release_company).where(release_company.c.release_id == active))
                complete = connection.scalar(select(func.count()).select_from(release_company_lineage).where(
                    release_company_lineage.c.release_id == active, release_company_lineage.c.assessment == "COMPLETE"))
                lineage = {"complete": complete, "not_assessed": members - complete}
            try:
                if active is None:
                    raise QueryUnavailable("no published company release")
                release = self._release(connection, active)
                ready, reason = True, None
            except QueryUnavailable as exc:
                release, ready, reason = None, False, str(exc)
            return {"active_release_id": active, "release": release, "projection_ready": ready,
                    "query_configuration_ready": len(self.cursors.secret) >= 32, "reason": reason,
                    "validity_status": "NOT_ASSESSED", "freshness_target": None,
                    "validity_generation": generation, "lineage_counts": lineage,
                    "source_validity_scope": "CAPTURED_DEPENDENCIES_ONLY", "newest_filings_assessed": False,
                    "numeric_invalidation_assessed": False, "official_financial_readiness": "NOT_ASSESSED",
                    "note": "Captured dependency assessment does not establish newest filings, financial currentness, or numerical invalidation."}

    @staticmethod
    def _history_release(connection, release_id, security_id):
        if type(security_id) is not int or security_id <= 0:
            raise InvalidQuery("security must be a positive integer")
        if release_id is not None and (type(release_id) is not int or release_id <= 0):
            raise InvalidQuery("release must be a positive integer")
        query = select(data_release).where(data_release.c.published_at.is_not(None))
        query = query.join(active_data_release, active_data_release.c.release_id == data_release.c.release_id) if release_id is None else query.where(data_release.c.release_id == release_id)
        release = connection.execute(query).mappings().one_or_none()
        if release is None:
            if release_id is None:
                raise QueryUnavailable("no published company release")
            raise RestartQuery("history release unavailable")
        member = connection.execute(select(release_company).where(release_company.c.release_id == release["release_id"],
            release_company.c.security_id == security_id)).mappings().one_or_none()
        if member is None:
            raise HistoryNotFound("security is not in this published release")
        return release, member

    @staticmethod
    def _history_timeout(connection):
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))

    def _history_page_identity(self, cursor, *, operation, security_id, release_id, fingerprint, limit, source=False):
        self.cursors.require_ready()
        if cursor is None:
            return None
        value = self.cursors.decode(cursor)
        keys = {"domain", "revision", "operation", "security", "pin", "fingerprint", "limit", "position"}
        if source:
            keys.add("fence")
        if (set(value) != keys or value["domain"] != "figure-history-page" or type(value["revision"]) is not int
                or value["revision"] != 1 or value["operation"] != operation or type(value["security"]) is not int or value["security"] != security_id
                or type(value["pin"]) is not int or value["pin"] <= 0 or value["fingerprint"] != fingerprint
                or type(value["limit"]) is not int or value["limit"] != limit
                or (release_id is not None and release_id != value["pin"])):
            raise InvalidQuery("history cursor does not match this request")
        return value

    def _history_page_token(self, *, operation, security, pin, fingerprint, limit, position, fence=None):
        payload = dict(domain="figure-history-page", revision=1, operation=operation, security=security,
                       pin=pin, fingerprint=fingerprint, limit=limit, position=position)
        if operation == "source-events":
            payload["fence"] = fence
        return self.cursors.encode(payload)

    @staticmethod
    def _history_window(connection, start):
        rows = connection.execute(text("""WITH RECURSIVE ancestry AS (
            SELECT release_id,parent_release_id,published_at,figure_history_revision,0 depth,ARRAY[release_id] path
            FROM data_release WHERE release_id=:start AND published_at IS NOT NULL
            UNION ALL SELECT p.release_id,p.parent_release_id,p.published_at,p.figure_history_revision,a.depth+1,a.path||p.release_id
            FROM ancestry a JOIN data_release p ON p.release_id=a.parent_release_id
            WHERE a.depth<299 AND p.published_at IS NOT NULL AND NOT p.release_id=ANY(a.path))
            SELECT * FROM ancestry ORDER BY depth"""), dict(start=start)).mappings().all()
        if not rows or any(row["parent_release_id"] in row["path"] for row in rows):
            raise QueryUnavailable("published history ancestry unavailable")
        if len(rows) < 300 and rows[-1]["parent_release_id"] is not None:
            raise QueryUnavailable("published history parent unavailable")
        return rows

    @staticmethod
    def _history_bundle_headers(connection, identifiers):
        identifiers = tuple({value for value in identifiers if value is not None})
        if not identifiers:
            return {}
        rows = connection.execute(select(exact_figure_bundle.c.bundle_id, exact_figure_bundle.c.security_id,
            exact_figure_bundle.c.snapshot_id, exact_figure_bundle.c.snapshot_sha256,
            exact_figure_bundle.c.engine_revision, exact_figure_bundle.c.contract_revision,
            exact_figure_bundle.c.sealed_at).where(exact_figure_bundle.c.bundle_id.in_(identifiers))).mappings()
        headers = {row["bundle_id"]: row for row in rows}
        if (len(headers) != len(identifiers) or any(row["sealed_at"] is None or row["contract_revision"] not in {1, 2}
                for row in headers.values())):
            raise QueryUnavailable("sealed exact history contract unavailable")
        return headers

    def _history_handles(self, base):
        return {kind: self.cursors.encode(dict(base, domain="figure-history-record", revision=1, kind=kind))
                for kind in ("inputs", "documents", "actions", "assumptions", "sources", "coverage")}

    def _history_endpoint(self, *, pin, release, security, member, node, headers):
        bundle = member["exact_bundle_id"] if member else None
        result = dict(release_id=release, security_id=security, snapshot_id=member["snapshot_id"] if member else None,
                      bundle_id=bundle, selection=None, supporting_records=None)
        if bundle is None:
            return dict(result, status="NOT_ASSESSED", reason="legacy-no-bundle" if member else "member-unavailable")
        header = headers[bundle]
        if header["security_id"] != security or header["snapshot_id"] != member["snapshot_id"]:
            raise QueryUnavailable("exact endpoint membership unavailable")
        if node is None:
            return dict(result, status="NOT_ASSESSED", reason="output-unavailable")
        digest = history_name_digest(node["node_key"])
        base = dict(pin=pin, release=release, security=security, bundle=bundle, root=digest, node=digest,
                    target="node", observation=None, parent=None, ordinal=None)
        return dict(result, status="SELECTED_IN_RELEASE", selection=history_selection_record(node),
                    graph_contract_revision=header["contract_revision"], supporting_records=self._history_handles(base))

    def figure_history(self, query: HistoryQuery, *, security_id, release_id=None, cursor=None, activations=False):
        operation = "activations" if activations else "timeline"
        fingerprint = query.fingerprint()
        identity = self._history_page_identity(cursor, operation=operation, security_id=security_id,
            release_id=release_id, fingerprint=fingerprint, limit=query.limit)
        if identity is not None and (type(identity["position"]) is not int or identity["position"] <= 0):
            raise InvalidQuery("invalid history ancestry position")
        with self._read_snapshot() as connection:
            self._history_timeout(connection)
            return self._figure_history_on_connection(connection, query=query, security_id=security_id,
                release_id=release_id, identity=identity, activations=activations, fingerprint=fingerprint)

    def _figure_history_on_connection(self, connection, *, query, security_id, release_id, identity, activations, fingerprint):
        operation = "activations" if activations else "timeline"
        release, pin_member = self._history_release(connection, identity["pin"] if identity else release_id, security_id)
        pin = release["release_id"]
        window = self._history_window(connection, identity["position"] if identity else pin)
        ids = [row["release_id"] for row in window]
        by_release = {row["release_id"]: row for row in window}
        position = {identifier: index for index, identifier in enumerate(ids)}
        if activations:
            selected = window[:query.limit]
            next_position = window[query.limit]["release_id"] if len(window) > query.limit else window[-1]["parent_release_id"]
        else:
            statement = select(figure_activation_transition).where(figure_activation_transition.c.release_id.in_(ids),
                figure_activation_transition.c.security_id == security_id,
                figure_activation_transition.c.metric_path == query.metric_path,
                figure_activation_transition.c.view == query.view, figure_activation_transition.c.period_kind == query.period_kind,
                figure_activation_transition.c.period_start == query.period_start,
                figure_activation_transition.c.period_end == query.period_end)
            events = sorted(connection.execute(statement).mappings().all(), key=lambda row: position[row["release_id"]])
            selected = events[:query.limit]
            next_position = events[query.limit]["release_id"] if len(events) > query.limit else window[-1]["parent_release_id"]
        needed_releases = {pin, *[row["release_id"] for row in selected], *[row["parent_release_id"] for row in selected if row["parent_release_id"] is not None]}
        members = {row["release_id"]: row for row in connection.execute(select(release_company).where(
            release_company.c.release_id.in_(needed_releases), release_company.c.security_id == security_id)).mappings()}
        headers = self._history_bundle_headers(connection, [row["exact_bundle_id"] for row in members.values()])
        current = FigureHistoryRepository.read_outputs_on_connection(connection, pin_member["exact_bundle_id"], query.selector()) if pin_member["exact_bundle_id"] is not None else None
        if activations:
            # One set query qualifies every output in this bounded ancestor window.
            bundle_ids = [row["exact_bundle_id"] for row in members.values() if row["exact_bundle_id"] is not None]
            nodes = connection.execute(text("""SELECT * FROM figure_selection WHERE bundle_id=ANY(:bundles)
                    AND metric_path=:metric AND view=:view AND COALESCE(record->>'purpose','OUTPUT')='OUTPUT'
                    AND record->'period'->>'kind'=:kind AND record->'period'->>'start' IS NOT DISTINCT FROM :start
                    AND record->'period'->>'end' IS NOT DISTINCT FROM :end"""),
                dict(bundles=bundle_ids, metric=query.metric_path, view=query.view, kind=query.period_kind,
                     start=str(query.period_start) if query.period_start else None, end=str(query.period_end) if query.period_end else None)).mappings().all() if bundle_ids else []
            by_bundle = {node["bundle_id"]: node for node in nodes}
            result_rows = [dict(release_id=row["release_id"], parent_release_id=row["parent_release_id"],
                activated_at=row["published_at"], hook_revision=row["figure_history_revision"],
                transition_coverage="NOT_ASSESSED" if row["figure_history_revision"] is None else "MECHANICAL_HOOK_EXECUTED",
                endpoint=self._history_endpoint(pin=pin, release=row["release_id"], security=security_id,
                    member=members.get(row["release_id"]), node=by_bundle.get((members.get(row["release_id"]) or {}).get("exact_bundle_id")), headers=headers)) for row in selected]
        else:
            refs = [(row[side + "_bundle_id"], row[side + "_node_key"]) for row in selected for side in ("previous", "replacement") if row[side + "_bundle_id"] is not None]
            nodes = FigureHistoryRepository.read_nodes_on_connection(connection, refs)
            result_rows = []
            for row in selected:
                record = {key: row[key] for key in ("transition_id", "release_id", "parent_release_id", "metric_path", "view", "period_kind", "period_start", "period_end", "value_text_changed", "state_changed", "provenance_changed", "endpoint_presence_changed", "declared_context_equal", "context_assessment", "reason", "contract_revision")}
                record.update(activated_at=by_release[row["release_id"]]["published_at"],
                    delta=dict(status="NOT_COMPUTED", value_decimal_text=None), cause=dict(status="UNKNOWN"))
                for side, endpoint_release in (("previous", row["parent_release_id"]), ("replacement", row["release_id"])):
                    node = nodes.get((row[side + "_bundle_id"], row[side + "_node_key"]))
                    record[side] = self._history_endpoint(pin=pin, release=endpoint_release, security=security_id,
                        member=members.get(endpoint_release), node=node, headers=headers)
                result_rows.append(public_history_record(record))
        endpoint = self._history_endpoint(pin=pin, release=pin, security=security_id, member=pin_member, node=current, headers=headers)
        coverage = FigureHistoryRepository.read_coverage_on_connection(connection, pin_member["exact_bundle_id"], current, query.metric_path, query.view) if pin_member["exact_bundle_id"] is not None else dict(assessment="NOT_ASSESSED", reason="legacy-no-bundle", earlier_history="UNKNOWN", scope="SUPPLIED_GRAPH_ONLY")
        return dict(history_contract_revision=1, release=dict(release_id=pin, parent_release_id=release["parent_release_id"], published_at=release["published_at"]),
            security_id=security_id, selector=public_history_record(query.selector()), endpoint=endpoint, coverage=coverage,
            financial_readiness="NOT_ASSESSED", earlier_history_complete=False, operation=operation,
            rows=result_rows, scanned_releases=len(window), traversal_complete=next_position is None,
            next_cursor=self._history_page_token(operation=operation, security=security_id, pin=pin,
                fingerprint=fingerprint, limit=query.limit, position=next_position) if next_position is not None else None)

    def _history_output_admission(self, connection, *, pin, release, security, bundle, root_digest):
        self._history_release(connection, pin, security)
        _, member = self._history_release(connection, release, security)
        if member["exact_bundle_id"] != bundle:
            raise HistoryNotFound("supporting root unavailable")
        header = self._history_bundle_headers(connection, [bundle])[bundle]
        if header["security_id"] != security or header["snapshot_id"] != member["snapshot_id"]:
            raise QueryUnavailable("exact endpoint membership unavailable")
        root = FigureHistoryRepository.read_node_digest_on_connection(connection, bundle, root_digest)
        if root is None or root["record"].get("purpose", "OUTPUT") != "OUTPUT":
            raise HistoryNotFound("supporting root unavailable")
        return root

    def figure_catalog(self, *, security_id, release_id=None, view="STRICT", limit=300, cursor=None):
        if view not in {"STRICT", "ASSUMED", "CONSERVATIVE"} or type(limit) is not int or not 1 <= limit <= 300:
            raise InvalidQuery("unsupported catalog view or page bound")
        fingerprint = history_name_digest(view)
        identity = self._history_page_identity(cursor, operation="catalog", security_id=security_id,
            release_id=release_id, fingerprint=fingerprint, limit=limit)
        after = identity["position"] if identity else None
        if after is not None and (not isinstance(after, str) or re.fullmatch(r"[0-9a-f]{64}", after) is None):
            raise InvalidQuery("invalid catalog continuation")
        with self._read_snapshot() as connection:
            self._history_timeout(connection)
            release, member = self._history_release(connection, identity["pin"] if identity else release_id, security_id)
            pin, bundle = release["release_id"], member["exact_bundle_id"]
            headers = self._history_bundle_headers(connection, [bundle])
            if bundle is None:
                if identity is not None:
                    raise HistoryNotFound("catalog continuation unavailable")
                nodes, coverage = [], {}
            else:
                header = headers[bundle]
                if header["security_id"] != security_id or header["snapshot_id"] != member["snapshot_id"]:
                    raise QueryUnavailable("exact endpoint membership unavailable")
                nodes = FigureHistoryRepository.read_catalog_on_connection(connection, bundle, view=view, after=after, limit=limit)
                coverage = FigureHistoryRepository.read_catalog_coverage_on_connection(connection, bundle, nodes[:limit])
            output = []
            for node in nodes[:limit]:
                record = history_selection_record(node)
                selector = {key: record[key] for key in ("metric_path", "view", "period")}
                handle = self.cursors.encode(dict(domain="figure-history-selector", revision=1, pin=pin,
                    security=security_id, bundle=bundle, root=history_name_digest(node["node_key"]), view=view))
                output.append(dict(selector=selector, selector_handle=handle, coverage=coverage[node["node_key"]],
                    endpoint=self._history_endpoint(pin=pin, release=pin, security=security_id, member=member, node=node, headers=headers)))
            return dict(history_contract_revision=1, operation="catalog", security_id=security_id,
                release=dict(release_id=pin, parent_release_id=release["parent_release_id"], published_at=release["published_at"]),
                view=view, rows=output, catalog_status="legacy-no-bundle" if bundle is None else "CAPTURED_OUTPUTS_ONLY",
                financial_readiness="NOT_ASSESSED", earlier_history_complete=False, display_association="NOT_ASSESSED",
                next_cursor=self._history_page_token(operation="catalog", security=security_id, pin=pin,
                    fingerprint=fingerprint, limit=limit, position=history_name_digest(nodes[limit - 1]["node_key"])) if len(nodes) > limit else None)

    def figure_selected(self, *, security_id, handle, operation="timeline", limit=300, cursor=None):
        if operation not in {"timeline", "activations"} or type(limit) is not int or not 1 <= limit <= 300:
            raise InvalidQuery("unsupported selected history operation or bound")
        value = self.cursors.decode(handle)
        if (set(value) != {"domain", "revision", "pin", "security", "bundle", "root", "view"}
                or value["domain"] != "figure-history-selector" or type(value["revision"]) is not int or value["revision"] != 1
                or any(type(value[key]) is not int or value[key] <= 0 for key in ("pin", "security", "bundle"))
                or type(security_id) is not int or value["security"] != security_id
                or value["view"] not in {"STRICT", "ASSUMED", "CONSERVATIVE"}
                or not isinstance(value["root"], str) or re.fullmatch(r"[0-9a-f]{64}", value["root"]) is None):
            raise InvalidQuery("invalid OUTPUT selector handle")
        with self._read_snapshot() as connection:
            self._history_timeout(connection)
            node = self._history_output_admission(connection, pin=value["pin"], release=value["pin"],
                security=security_id, bundle=value["bundle"], root_digest=value["root"])
            if node["view"] != value["view"]:
                raise HistoryNotFound("selected view unavailable")
            record = history_selection_record(node)
            period = record["period"]
            query = HistoryQuery(metric_path=node["metric_path"], view=node["view"], period_kind=period["kind"],
                period_start=date.fromisoformat(period["start"]) if period.get("start") else None,
                period_end=date.fromisoformat(period["end"]) if period.get("end") else None, limit=limit)
            fingerprint = query.fingerprint()
            identity = self._history_page_identity(cursor, operation=operation, security_id=security_id,
                release_id=value["pin"], fingerprint=fingerprint, limit=limit)
            if identity is not None and (type(identity["position"]) is not int or identity["position"] <= 0):
                raise InvalidQuery("invalid history ancestry position")
            return self._figure_history_on_connection(connection, query=query, security_id=security_id,
                release_id=value["pin"], identity=identity, activations=operation == "activations", fingerprint=fingerprint)

    def _history_support_admission(self, connection, handle):
        value = self.cursors.decode(handle)
        keys = {"domain", "revision", "kind", "pin", "release", "security", "bundle", "root", "node", "target", "observation", "parent", "ordinal"}
        if (set(value) != keys or value["domain"] != "figure-history-record" or type(value["revision"]) is not int or value["revision"] != 1
                or value["kind"] not in {"inputs", "documents", "actions", "assumptions", "sources", "coverage"}
                or value["target"] not in {"node", "observation"}
                or any(type(value[key]) is not int or value[key] <= 0 for key in ("pin", "release", "security", "bundle"))
                or any(not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None for key in ("root", "node"))):
            raise InvalidQuery("invalid supporting record handle")
        root = self._history_output_admission(connection, pin=value["pin"], release=value["release"],
            security=value["security"], bundle=value["bundle"], root_digest=value["root"])
        node = root if value["node"] == value["root"] else FigureHistoryRepository.read_node_digest_on_connection(connection, value["bundle"], value["node"])
        if node is None:
            raise HistoryNotFound("supporting root unavailable")
        if value["parent"] is None:
            if value["target"] != "node" or value["node"] != value["root"] or value["ordinal"] is not None or value["observation"] is not None:
                raise HistoryNotFound("supporting edge unavailable")
        else:
            if (not isinstance(value["parent"], str) or re.fullmatch(r"[0-9a-f]{64}", value["parent"]) is None
                    or type(value["ordinal"]) is not int or value["ordinal"] < -1):
                raise InvalidQuery("invalid supporting edge")
            parent = node if value["parent"] == value["node"] else FigureHistoryRepository.read_node_digest_on_connection(connection, value["bundle"], value["parent"])
            if parent is None:
                raise HistoryNotFound("supporting edge unavailable")
            if value["ordinal"] == -1:
                edge = dict(observation_id=parent["observation_id"], input_node_key=None)
            else:
                edge = connection.execute(select(figure_dependency).where(figure_dependency.c.bundle_id == value["bundle"],
                    figure_dependency.c.parent_node_key == parent["node_key"], figure_dependency.c.ordinal == value["ordinal"])).mappings().one_or_none()
            if edge is None or (value["target"] == "node" and (edge["input_node_key"] != node["node_key"] or value["observation"] is not None)) or (value["target"] == "observation" and (type(value["observation"]) is not int or edge["observation_id"] != value["observation"] or value["node"] != value["parent"])):
                raise HistoryNotFound("supporting edge unavailable")
        if value["target"] == "observation":
            target = FigureHistoryRepository.read_observations_on_connection(connection, [value["observation"]]).get(value["observation"])
            if target is None:
                raise HistoryNotFound("supporting observation unavailable")
        else:
            target = node
        return value, node, target

    def figure_support(self, *, security_id, handle, limit=300, cursor=None):
        if type(security_id) is not int or security_id <= 0:
            raise InvalidQuery("security must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 300:
            raise InvalidQuery("history limit must be1–300")
        fingerprint = history_name_digest(handle)
        identity = self._history_page_identity(cursor, operation="support", security_id=security_id,
            release_id=None, fingerprint=fingerprint, limit=limit)
        with self._read_snapshot() as connection:
            self._history_timeout(connection)
            value, node, target = self._history_support_admission(connection, handle)
            if value["security"] != security_id or identity is not None and identity["pin"] != value["pin"]:
                raise HistoryNotFound("supporting root unavailable")
            kind = value["kind"]
            position = identity["position"] if identity else None
            if kind == "inputs":
                after = -2 if position is None else position
                if type(after) is not int or after < -2:
                    raise InvalidQuery("invalid input continuation")
                rows = FigureHistoryRepository.read_inputs_on_connection(connection, node, after=after, limit=limit) if value["target"] == "node" else []
                observations = FigureHistoryRepository.read_observations_on_connection(connection, [row["observation_id"] for row in rows[:limit] if row["observation_id"] is not None])
                children = FigureHistoryRepository.read_nodes_on_connection(connection, [(value["bundle"], row["input_node_key"]) for row in rows[:limit] if row["input_node_key"] is not None])
                output = []
                for row in rows[:limit]:
                    result = dict(ordinal=row["ordinal"], **{key: public_history_record(row["record"].get(key)) for key in ("role", "constant_decimal_text", "coefficient_decimal_text")})
                    result["role"] = row.get("role") or result["role"]
                    child = dict(value, parent=value["node"], ordinal=row["ordinal"])
                    if row["observation_id"] is not None:
                        result.update(observation=history_observation_record(observations[row["observation_id"]]),
                            supporting_records=self._history_handles(dict(child, target="observation", observation=row["observation_id"])))
                    elif row["input_node_key"] is not None:
                        actual = children[(value["bundle"], row["input_node_key"])]
                        result.update(selection=history_selection_record(actual),
                            supporting_records=self._history_handles(dict(child, node=history_name_digest(actual["node_key"]), target="node", observation=None)))
                    output.append(result)
                next_position = rows[limit - 1]["ordinal"] if len(rows) > limit else None
            elif kind in {"documents", "actions", "assumptions"}:
                if position is not None and (not isinstance(position, str) or re.fullmatch(r"[0-9a-f]{64}", position) is None):
                    raise InvalidQuery("invalid metadata continuation")
                rows = FigureHistoryRepository.read_metadata_on_connection(connection, value["bundle"], target, kind=kind, after=position, limit=limit)
                output = [row["record"] for row in rows[:limit]]
                next_position = history_name_digest(rows[limit - 1]["key"]) if len(rows) > limit else None
            elif kind == "sources":
                if position is not None and (not isinstance(position, list) or len(position) != 2 or any(type(i) is not int or i <= 0 for i in position)):
                    raise InvalidQuery("invalid source continuation")
                rows = FigureHistoryRepository.read_sources_on_connection(connection, bundle_id=value["bundle"], release_id=value["release"], security_id=security_id, after=position, limit=limit)
                output = rows[:limit]
                next_position = [rows[limit - 1]["source_item_id"], rows[limit - 1]["source_observation_id"]] if len(rows) > limit else None
            else:
                if position is not None and (not isinstance(position, str) or re.fullmatch(r"[0-9a-f]{64}", position) is None):
                    raise InvalidQuery("invalid coverage continuation")
                rows = FigureHistoryRepository.read_coverage_paths_on_connection(connection, value["bundle"], node, after=position, limit=limit)
                output = [row["record"] for row in rows[:limit]]
                next_position = history_name_digest(rows[limit - 1]["key"]) if len(rows) > limit else None
            return dict(history_contract_revision=1, security_id=security_id, release_id=value["pin"], endpoint_release_id=value["release"],
                bundle_id=value["bundle"], root_identity=value["root"], target=value["target"], kind=kind, rows=output,
                supporting_status="QUALIFIED_DIRECT_RECORDS" if output else "NO_QUALIFIED_RECORDS",
                selected_reason=public_history_record(target["record"].get("reason")), transitive_expansion_complete=False,
                next_cursor=self._history_page_token(operation="support", security=security_id, pin=value["pin"],
                    fingerprint=fingerprint, limit=limit, position=next_position) if next_position is not None else None)

    def figure_source_events(self, *, security_id, source_item_id, release_id=None, limit=300, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 300 or type(source_item_id) is not int or source_item_id <= 0:
            raise InvalidQuery("source item and limit must be positive and limit at most300")
        fingerprint = history_name_digest(str(source_item_id))
        identity = self._history_page_identity(cursor, operation="source-events", security_id=security_id,
            release_id=release_id, fingerprint=fingerprint, limit=limit, source=True)
        if identity is not None and any(type(identity[key]) is not int or identity[key] <= 0 for key in ("position", "fence")):
            raise InvalidQuery("invalid source journal continuation")
        with self._read_snapshot() as connection:
            self._history_timeout(connection)
            release, member = self._history_release(connection, identity["pin"] if identity else release_id, security_id)
            binding = FigureHistoryRepository.read_journal_binding_on_connection(connection, release["release_id"], security_id, source_item_id, member["exact_bundle_id"])
            if binding is None:
                raise HistoryNotFound("source item is not a dependency of this published member")
            latest = connection.scalar(select(source_check_occurrence.c.occurrence_id).where(source_check_occurrence.c.source_item_id == source_item_id).order_by(source_check_occurrence.c.occurrence_id.desc()).limit(1))
            if identity is not None and identity["fence"] != latest:
                raise RestartQuery("source checks changed; restart this journal")
            rows = FigureHistoryRepository.read_journal_on_connection(connection, source_item_id, after=identity["position"] if identity else None, limit=limit)
            next_position = rows[limit - 1]["occurrence_id"] if len(rows) > limit else None
            return dict(history_contract_revision=1, release_id=release["release_id"], security_id=security_id,
                source_item_id=source_item_id, binding=binding, occurrence_fence=latest,
                journal_coverage="CAPTURED_CHECKS_ONLY" if latest is not None else "NOT_ASSESSED",
                affected_figures_assessed=False, financial_readiness="NOT_ASSESSED", cause=dict(status="UNKNOWN"),
                rows=rows[:limit], next_cursor=self._history_page_token(operation="source-events", security=security_id,
                    pin=release["release_id"], fingerprint=fingerprint, limit=limit, position=next_position, fence=latest) if next_position is not None else None)
