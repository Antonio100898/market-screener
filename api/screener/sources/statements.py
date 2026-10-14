"""Layer 1: the financial statements as a reader sees them, not as XBRL reports them.

Every other check in this project compares one machine reading against another.
This one goes to the document: the balance sheet and income statement the company
actually published, rendered by SEC's own viewer, read as labelled rows of money.
It is the only evidence that does not share an ancestor with the number it checks —
if the tagged `Assets` fact and the "Total assets" line of the printed balance
sheet agree, the figure is right in the sense a person would mean.

`FilingSummary.xml` lists every rendered report in a filing with its short name,
so the balance sheet is found by name rather than by guessing at R-numbers, which
differ between filers and between years.

Two things the rendering carries and the XBRL does not:

  * the scale. The table header says "$ in Millions" and every cell is then a
    number of millions. Reading 45,468 as dollars instead of millions is the
    difference between a company and a corner shop.
  * the sign convention. A parenthesised cell is negative, and a label ending in
    a minus-bearing concept may be presented positive.
"""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from calendar import monthrange
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, InvalidOperation, Overflow, Rounded, Underflow, localcontext
from html.parser import HTMLParser

SUMMARY_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/FilingSummary.xml"
REPORT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{file}"

# What each statement is called. Filers name them differently — "CONSOLIDATED
# BALANCE SHEETS", "Consolidated Statements of Financial Position", "Consolidated
# Balance Sheet (unaudited)" — so a statement is recognised by the words that
# survive all of those, and parenthetical suffixes are ignored.
STATEMENTS = {
    "balance_sheet": ("balance sheet", "financial position", "financial condition"),
    # Small filers combine the income statement with comprehensive income and title
    # it accordingly; Creatd's is "Consolidated Statements of Comprehensive Loss".
    # Ranked last, so a filer publishing both keeps the pure one.
    "cash_flow": ("statements of cash flows", "statement of cash flows",
                  "statements of cash flow", "cash flows"),
    "income": ("statements of operations", "statement of operations",
               "statements of income", "statement of income", "statements of earnings",
               "statement of earnings", "results of operations",
               "comprehensive loss", "comprehensive income"),
}
# A "(Parenthetical)" report carries the share counts and par values that sit in
# the margin of the statement, not the statement itself.
PARENTHETICAL = "parenthetical"
# ...except when it is the thing being read: the share counts and par values live
# in the margin of the balance sheet, on its parenthetical companion.
STATEMENTS_PARENTHETICAL = ("balance sheet", "financial position")

_UNITS = {"thousands": Decimal("1e3"), "millions": Decimal("1e6"), "billions": Decimal("1e9")}
# The money scale is the one attached to the dollar sign. CoStar's header reads
# "$ in Thousands, shares in Millions", and a pattern that merely looks for the
# word "millions" finds the share clause and reads every figure a thousand times
# too large.
_BARE_SCALE = re.compile(r"(?<!shares )(?<!share )\bin\s+(thousands|millions|billions)", re.I)
_SHARES_UNSCALED = re.compile(r"shares?\s+in\s+(thousands|millions|billions)", re.I)
# A row counting shares, not one naming the people who hold them. "Net Income to
# Shareholders" contains the word "share" and is money; scaling it as a share count
# left Markel's $2.1bn of profit reading as $2,107,010.
_A_COUNT_OF_SHARES = re.compile(r"\bshares?\b(?!\s*holder)", re.I)
# A per-share figure is already in dollars and takes no scale at all. Coca-Cola's
# statement is headed "$ in Millions" and prints diluted earnings of 3.04; scaling
# that as money makes it $3,040,000 a share.
# "(in dollars per share)", "(in shares)" — the renderer's note of the unit, not
# part of what the line is called. JPM heads its earnings "Diluted earnings per
# share (in dollars per share)" and Apple heads the same line just "Diluted".
_UNIT_SUFFIX = re.compile(r"\s*\((?:in\s+)?(?:dollars|usd|shares|dollars per share)"
                          r"(?:\s+per\s+share)?\)\s*$", re.I)
_IN_SHARES = re.compile(r"\(\s*in\s+shares\s*\)", re.I)
_PER_SHARE = re.compile(r"\bper\s+(?:basic\s+|diluted\s+|common\s+|ordinary\s+)?(?:share|unit)", re.I)
# The sign comes from the parentheses and from nothing else. A label is not
# evidence: Intellicheck heads its line "Net loss" and prints "$ 1,273" beside a
# prior year of "$ (918)" — a stale caption over a real profit, which the page's own
# arithmetic confirms (pre-tax 1,331 less tax 58). Reading the caption as a sign
# inverted a profitable year into a loss.
# A money cell, in any of the orders the renderer uses: "1,296", "$ 1,296",
# "(52)", "$ (52)". The dollar sign may sit outside the bracket or inside it, and
# a pattern that fixes the order silently skips every loss — Hyatt's "$ (52)" was
# passed over and the next column's $1,296 profit read in its place.
_CELL = re.compile(r"^[\$£€¥￥₨]?\s*\(?\s*[\$£€¥￥₨]?\s*([+−-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+))\s*\)?$")
# An "attributable to" line belonging to somebody other than the parent: the
# minority holders, or a per-share figure. "Attributable to common stockholders" is
# NOT excluded — for Cohen & Steers and for Vivid Seats' Class A that phrasing IS
# the parent's own line, and skipping it took the consolidated figure instead.
_NOT_THE_PARENT = re.compile(
    r"noncontrolling|non-controlling|minority|per share|per unit|preferred|redeemable"
    # an intermediate holding company is not the registrant: Ares prints "attributable
    # to Ares Operating Group entities" above its own line, $307M larger
    r"|operating group|operating partnership|operating compan"
    # and "attributable to common stockholders" is claimed by two different figures,
    # so it is matched by its own phrase further down the list, never by the prefix
    r"|common stockholders|common shareholders", re.I)


def reports(summary_xml: str) -> list[dict]:
    """Every rendered report in the filing: {file, name, category}.

    `MenuCategory` is SEC's own division of a filing into "Statements", "Notes",
    "Details" and the rest. It is the difference between the income statement and a
    note that happens to mention operations, and matching on the name alone reached
    for R71 of Creatd's 10-K — a note eighty reports past the statement itself.
    """
    out = []
    for block in re.findall(r"<Report[^>]*>(.*?)</Report>", summary_xml, re.S | re.I):
        def field(name):
            m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S | re.I)
            return html.unescape(m.group(1)).strip() if m else ""
        file, name = field("HtmlFileName") or field("XmlFileName"), field("ShortName")
        if file and name:
            out.append({"file": file, "name": name, "category": field("MenuCategory")})
    return out


def find(summary_xml: str, kind: str) -> str | None:
    """The rendered file holding one statement, chosen by its printed name.

    The main statement wins over its parenthetical companion, and the first match
    wins over later ones — a filing that restates a segment later in the document
    still opens with the consolidated statement.
    """
    wanted = STATEMENTS[kind]
    found = reports(summary_xml)
    primary = [r for r in found if r.get("category") == "Statements"] or found
    # phrase order carries the preference, so the whole set is searched for the
    # first phrase before the second is tried
    for phrase in wanted:
        for report in primary:
            name = report["name"].lower()
            if PARENTHETICAL in name:
                continue
            if phrase in name:
                return report["file"]
    return None


def _readable_head(document: str) -> str:
    """The first of the document a reader sees, with the machinery removed.

    The rendered reports carry a block of script and comments before the table, and
    it mentions units of its own. CoStar's income statement is headed "$ in
    Thousands" while the preamble contains the word "Millions", so scanning raw
    characters read every figure a thousand times too large.
    """
    body = re.sub(r"<script[^>]*>.*?</script>", " ", document, flags=re.S | re.I)
    body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))[:600]


def scale(document: str) -> Decimal | None:
    """The multiplier the table header declares, or 1."""
    parsed = read(document)
    return parsed.tables[0].money_scale if parsed.tables else Decimal(1)


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)]


_HEADER_DATE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _duration(value: str) -> int | None:
    if not re.fullmatch(r"[+\-−]?[0-9]+", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Header:
    column: int
    text: str
    end: date | None
    currency: str | None
    money_scale: Decimal | None
    year: int | None
    duration_months: int | None
    start: date | None = None
    duration_weeks: int | None = None
    period_conflict: bool = False


@dataclass(frozen=True)
class Cell:
    column: int
    physical_column: int
    lexical: str
    status: str
    raw: Decimal | None
    multiplier: Decimal | None
    value: Decimal | None
    currency: str | None
    column_span: int


@dataclass(frozen=True)
class Row:
    table: int
    position: int
    label: str
    label_cells: tuple[str, ...]
    concept: str | None
    section: str
    basis: str | None
    measurement: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class Table:
    position: int
    headers: tuple[Header, ...]
    rows: tuple[Row, ...]
    active: tuple[int, ...]
    money_scale: Decimal | None


@dataclass(frozen=True)
class Statement:
    tables: tuple[Table, ...]


class _Tables(HTMLParser):
    """Collect physical cells; nested definition tables keep their own identity."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.tables = []
        self.sequence = 0
        self.hidden = 0
        self.outside = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "table":
            self.stack.append({"position": self.sequence, "rows": [], "row": None, "cell": None, "thead": False,
                               "caption": " ".join(self.outside) if not self.stack else ""})
            self.outside = []
            self.sequence += 1
        if not self.stack:
            return
        frame = self.stack[-1]
        if tag == "thead":
            frame["thead"] = True
        elif tag == "tr":
            frame["row"] = []
        elif tag in ("td", "th") and frame["row"] is not None:
            attributes = dict(attrs)
            def span(name):
                try:
                    return max(1, min(1000, int(attributes.get(name, 1))))
                except (ValueError, TypeError):
                    return 1
            frame["cell"] = {"text": [], "header": frame["thead"] or (tag == "th" and attributes.get("scope") != "row"), "colspan": span("colspan"),
                             "rowspan": span("rowspan"), "concept": None}
        cell = frame["cell"]
        if cell is not None:
            match = _ELEMENT.search(" ".join(value or "" for _, value in attrs))
            if match:
                cell["concept"] = match.group(1)
            if tag in ("br", "p", "div"):
                cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.stack:
            return
        frame = self.stack[-1]
        if tag == "thead":
            frame["thead"] = False
        elif tag in ("td", "th") and frame["cell"] is not None:
            cell = frame["cell"]
            cell["text"] = re.sub(r"\s+", " ", "".join(cell["text"])).strip()
            frame["row"].append(cell)
            frame["cell"] = None
        elif tag == "tr" and frame["row"] is not None:
            frame["rows"].append(frame["row"])
            frame["row"] = None
        elif tag == "table":
            self.tables.append(self.stack.pop())

    def handle_data(self, text):
        if self.stack and not self.hidden and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"]["text"].append(text)
        elif not self.stack and not self.hidden:
            self.outside.append(text)


def _number(lexical: str) -> tuple[str, Decimal | None]:
    if not lexical:
        return "BLANK", None
    if lexical.lower() in ("-", "—", "–", "nil", "n/a"):
        return "NIL", None
    match = _CELL.fullmatch(lexical)
    # Parentheses must be balanced and cannot add a second sign to a signed token.
    if not match or lexical.count("(") != lexical.count(")") or (
            "(" in lexical and match.group(1).startswith(("-", "+", "−"))) or (
            sum(lexical.count(symbol) for symbol in "$£€¥￥₨") > 1):
        return "UNPARSED", None
    try:
        raw = Decimal(match.group(1).replace(",", "").replace("−", "-"))
        return "NUMERIC", raw.copy_negate() if "(" in lexical else raw
    except InvalidOperation:
        return "UNPARSED", None


_CURRENCY = re.compile(r"\b(USD|EUR|GBP|JPY|CNY|RMB|INR|CAD|AUD|CHF|HKD|SGD|KRW|TWD|BRL|MXN|ZAR|SEK|NOK|DKK|NZD|ILS|PLN)\b")
_MONEY_UNITS = re.compile(r"(?:[\$£€¥￥₨]|\b[A-Z]{3}|\bmoney)\s+in\s+(thousands|millions|billions)\b", re.I)
_SCALED_UNIT = re.compile(r"([\$£€¥￥₨]|\b[A-Z]{3})\s+in\s+(thousands|millions|billions)\b", re.I)
_DECLARED_SCALE = re.compile(r"(?:[\$£€¥￥₨]|\b[A-Z]{3}|\bmoney|\bdollars)\s+in\s+(\w+)", re.I)
_DECLARED_SHARES = re.compile(r"\bshares?\s+in\s+(\w+)", re.I)
_CENT_SHARE = re.compile(r"(?:cents|pence)\s+per\s+(?:ordinary\s+)?share|per\s+(?:ordinary\s+)?share\s*(?:\(\s*)?(?:in\s+)?(?:cents|pence)", re.I)
_DOLLAR_SHARE = re.compile(r"dollars?\s+per\s+(?:ordinary\s+)?share|per\s+(?:ordinary\s+)?share\s*\(\s*(?:in\s+)?dollars?\s*\)", re.I)


def _currency(text: str) -> str | None:
    currencies = {"CNY" if value == "RMB" else value for value in _CURRENCY.findall(text)}
    return next(iter(currencies)) if len(currencies) == 1 else None


def _money_scale(head: str) -> Decimal | None:
    if any(match.group(1).lower() not in _UNITS for match in _DECLARED_SCALE.finditer(head)):
        return None
    found = {_UNITS[m.group(1).lower()] for m in _MONEY_UNITS.finditer(head)}
    if not found:
        # A bare scale is monetary only when it is not a share-count clause.
        without_shares = _SHARES_UNSCALED.sub("", head)
        found = {_UNITS[m.group(1).lower()] for m in _BARE_SCALE.finditer(without_shares)}
    return next(iter(found)) if len(found) == 1 else None if found else Decimal(1)


def _column_money_scale(head: str, column_text: str) -> Decimal | None:
    shared = _money_scale(head)
    if shared is not None:
        return shared
    # Distinct scales need the unit actually printed over this physical column.
    found = {_UNITS[match.group(2).lower()] for match in _SCALED_UNIT.finditer(head)
             if match.group(1) in column_text}
    return next(iter(found)) if len(found) == 1 else None


def _measurement(label: str, concept: str | None, section: str) -> str:
    name = concept or ""
    if "SharesOutstanding" in name or "WeightedAverageNumber" in name:
        return "SHARES"
    if re.search(r"Per(?:Basic|Diluted|Common)?Share", name) or _PER_SHARE.search(label):
        return "PER_SHARE"
    if _IN_SHARES.search(label) or _A_COUNT_OF_SHARES.search(label):
        return "SHARES"
    if label.lower() in ("basic", "diluted"):
        if "weighted" in section.lower() or "number of shares" in section.lower():
            return "SHARES"
        if _PER_SHARE.search(section) or "earningspershare" in section.lower():
            return "PER_SHARE"
        return "UNKNOWN"
    return "MONEY"


def _per_share_scale(text: str) -> Decimal | None:
    cents = bool(_CENT_SHARE.search(text) or re.search(r"per\s+(?:ordinary\s+)?share.*\(\s*(?:in\s+)?(?:cents|pence)\s*\)", text, re.I))
    dollars = bool(_DOLLAR_SHARE.search(text))
    return None if cents and dollars else Decimal("0.01") if cents else Decimal(1)


def _grid(rows):
    pending = {}
    out = []
    for row in rows:
        occupied = {column: cell for column, (remaining, cell) in pending.items()}
        pending = {column: (remaining - 1, cell) for column, (remaining, cell) in pending.items()
                   if remaining > 1}
        column = 0
        for cell in row:
            while column in occupied:
                column += 1
            for offset in range(cell["colspan"]):
                occupied[column + offset] = cell
                if cell["rowspan"] > 1:
                    pending[column + offset] = (cell["rowspan"] - 1, cell)
            column += cell["colspan"]
        out.append(occupied)
    return out


def read(document: str | Statement) -> Statement:
    """Read once without choosing an annual source, receipt basis or ISO from a symbol."""
    if isinstance(document, Statement):
        return document
    parser = _Tables()
    parser.feed(document)
    tables = []
    for frame in sorted(parser.tables, key=lambda table: table["position"]):
        grid = _grid(frame["rows"])
        header_rows = []
        for row in grid:
            if any(cell["header"] for cell in row.values()):
                header_rows.append(row)
            elif header_rows or row:
                break
        prefix = re.sub(r"\s+", " ", frame["caption"]).strip()
        shared_head = " ".join([prefix] + [row.get(0, {}).get("text", "") for row in header_rows])
        head = " ".join([prefix] + [" ".join(cell["text"] for cell in row.values())
                                      for row in header_rows])
        money_factor = _money_scale(head)
        share_scales = {_UNITS[m.group(1).lower()] for m in _SHARES_UNSCALED.finditer(head)}
        share_factor = next(iter(share_scales)) if len(share_scales) == 1 else None if share_scales else Decimal(1)
        if any(match.group(1).lower() not in _UNITS for match in _DECLARED_SHARES.finditer(head)):
            share_factor = None
        width = max((max(row, default=0) for row in grid), default=0)
        label_spans = {row[0]["colspan"] for row in grid if 0 in row
                       and not any(cell["header"] for cell in row.values())
                       and any(_number(cell["text"])[0] == "NUMERIC" for key, cell in row.items() if key > 0)}
        label_width = next(iter(label_spans)) if len(label_spans) == 1 else 1
        # SEC may reserve a separate footnote column beneath its row-spanning
        # statement caption. It belongs to the label area, not a dated amount.
        if header_rows and 0 in header_rows[0] and header_rows[0][0]["rowspan"] > 1:
            label_width = max(label_width, header_rows[0][0]["colspan"])
        numeric_width = max(0, width + 1 - label_width)
        headers = []
        for column in range(label_width, width + 1):
            pieces = [row[column]["text"] for row in header_rows if column in row]
            text = " ".join(dict.fromkeys(pieces))
            ends = set()
            date_year_spans = set()
            for match in _HEADER_DATE.finditer(text):
                month, day, year = match.groups()
                date_year_spans.add(match.span(3))
                try:
                    ends.add(date(int(year), _MONTHS[month[:3].lower()], int(day)))
                except ValueError:
                    pass
            for year, month, day in re.findall(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text):
                try:
                    ends.add(date(int(year), int(month), int(day)))
                except ValueError:
                    pass
            ordered = sorted(ends)
            explicit_range = len(ordered) == 2 and bool(re.search(r"\b(?:to|through)\b", text, re.I))
            explicit = _currency(text)
            # Only an explicit single statement currency may fill a bare date header.
            currencies = set(_CURRENCY.findall(text))
            years = {int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", text)}
            durations = {None if match.span(1) in date_year_spans else _duration(match.group(1))
                         for match in re.finditer(r"(\S*?)\s*Months?\s+Ended\b", text, re.I)}
            if re.search(r"\bYears?\s+Ended\b", text, re.I):
                durations.add(12)
            weeks = {None if match.span(1) in date_year_spans else _duration(match.group(1))
                     for match in re.finditer(r"(\S*?)\s*Weeks?\s+Ended\b", text, re.I)}
            headers.append(Header(column - label_width, text, ordered[-1] if explicit_range else next(iter(ends)) if len(ends) == 1 else None,
                                  explicit if currencies else _currency(head), _column_money_scale(head, text),
                                  next(iter(years)) if len(years) == 1 else None,
                                  next(iter(durations)) if len(durations) == 1 else None,
                                  start=ordered[0] if explicit_range else None,
                                  duration_weeks=next(iter(weeks)) if len(weeks) == 1 else None,
                                  period_conflict=(len(durations) > 1 or len(weeks) > 1
                                                   or any(value is None or value <= 0 for value in durations | weeks))))
        rows = []
        section = ""
        for position, physical in enumerate(grid):
            if any(cell["header"] for cell in physical.values()) or not physical:
                continue
            label_cell = physical.get(0, {})
            label = label_cell.get("text", "").rstrip(" :")
            concept = label_cell.get("concept")
            tokens = [physical.get(column, {}).get("text", "") for column in range(label_width, width + 1)]
            if all(not token for token in tokens) and label and (not concept or concept.endswith("Abstract")):
                section = label
            measurement = _measurement(label, concept, section)
            factor = share_factor if measurement == "SHARES" else None
            cells = []
            for column, token in enumerate(tokens):
                status, raw = _number(token)
                multiplier = (headers[column].money_scale if measurement == "MONEY" else
                              _per_share_scale(" ".join((label, section, shared_head, headers[column].text)))
                              if measurement == "PER_SHARE" else factor)
                span = physical.get(column + label_width, {}).get("colspan", 1)
                cells.append(Cell(column, column + label_width, token, status, raw, multiplier,
                                  raw * multiplier if raw is not None and multiplier is not None and span == 1 else None,
                                  None if measurement == "SHARES" else headers[column].currency, span))
            basis = "RECEIPT" if re.search(r"\bADS\b|deposit[ao]ry", label + " " + section, re.I) else (
                "ORDINARY" if re.search(r"\bequity share|\bordinary", label + " " + section, re.I) else None)
            if measurement not in ("SHARES", "PER_SHARE"):
                basis = None
            label_cells = tuple(physical.get(column, {}).get("text", "") for column in range(label_width))
            rows.append(Row(frame["position"], position, label, label_cells, concept, section, basis, measurement, tuple(cells)))
        # Rejected/nil cells are evidence of a physical column too. Only globally
        # blank columns may disappear from the compatibility projection.
        active = tuple(column for column in range(numeric_width)
                       if any(row.cells[column].status != "BLANK" for row in rows))
        tables.append(Table(frame["position"], tuple(headers), tuple(rows), active, money_factor))
    return Statement(tuple(tables))


class PrintedLine(tuple):
    """The existing two-item view retains its owning row's unit context."""
    def __new__(cls, row: Row, positions, table: Table, values=None):
        value = super().__new__(cls, (row.label, [row.cells[position].value for position in positions] if values is None else values))
        value.row = row
        value.cells = tuple(row.cells[position] for position in positions)
        value.headers = tuple(table.headers[position] for position in positions)
        value.table = table
        value.positions = positions
        return value


def columns(document: str | Statement) -> list[date | None]:
    """Periods at the same physical positions used by lines and elements."""
    parsed = read(document)
    for table in parsed.tables:
        if table.active and any(header.end is not None for header in table.headers):
            return [table.headers[position].end for position in table.active]
    return []


def lines(document: str | Statement) -> list[PrintedLine]:
    """Nullable cells never slide into a neighboring period."""
    return [PrintedLine(row, table.active, table) for table in read(document).tables
            for row in table.rows if (row.label or row.concept) and _number(row.label)[0] != "NUMERIC"
            and any(cell.raw is not None for cell in row.cells)]


@dataclass(frozen=True)
class SourceContext:
    end: date
    start: date | None
    measurement: str
    currency: str | None
    basis: str | None = None


def period_start(header: Header) -> date | None:
    if header.period_conflict or header.end is None:
        return None
    starts = {header.start} if header.start is not None else set()
    if header.duration_weeks is not None:
        if not 0 < header.duration_weeks * 7 <= (header.end - date.min).days + 1:
            return None
        starts.add(header.end - timedelta(days=7 * header.duration_weeks - 1))
    if header.duration_months is not None:
        months = header.end.year * 12 + header.end.month - 1 - header.duration_months
        if header.duration_months <= 0 or months < 12:
            return None
        year, month = divmod(months, 12)
        starts.add(date(year, month + 1, min(header.end.day, monthrange(year, month + 1)[1])) + timedelta(days=1))
    return next(iter(starts)) if len(starts) == 1 else None


def qualify(statement_lines, context: SourceContext):
    """Keep numeric witnesses only in the selected source's physical context."""
    qualified = []
    for line in statement_lines:
        if not isinstance(line, PrintedLine):
            continue
        values = []
        for cell, header in zip(line.cells, line.headers):
            start = period_start(header)
            has_duration = any(value is not None for value in
                               (header.start, header.duration_months, header.duration_weeks))
            agrees = (cell.status == "NUMERIC" and cell.value is not None
                      and not header.period_conflict
                      and (not has_duration or start is not None)
                      and line.row.measurement == context.measurement
                      and header.end == context.end
                      and (context.start is None or start == context.start)
                      and (context.measurement == "SHARES" or cell.currency == context.currency)
                      and (context.basis is None or line.row.basis == context.basis))
            values.append(cell.value if agrees else None)
        qualified.append(PrintedLine(line.row, line.positions, line.table, values))
    return qualified


@dataclass(frozen=True)
class LiabilityDeductionWitness:
    kind: str
    assessment: str
    context: SourceContext
    concept: str
    table: Table
    row: Row
    cell: Cell
    header: Header
    comparand: Decimal
    supporting_rows: tuple[Row, ...]
    equations: tuple[str, ...]
    native_scope: str = "NOT_ASSESSED"
    native_sign_role: str = "NOT_ASSESSED"


def liability_deduction_witness(
    printed, context: SourceContext, *, concept: str
) -> LiabilityDeductionWitness | None:
    """Prove a literal net-assets deduction without consulting a compared amount."""
    if (concept != "ifrs-full:CurrentLiabilities" or context.start is not None
            or context.measurement != "MONEY" or context.basis is not None
            or not isinstance(context.end, date) or not isinstance(context.currency, str)
            or not _CURRENCY.fullmatch(context.currency) or context.currency == "RMB"):
        return None
    concepts = ("ifrs-full_Assets", "ifrs-full_CurrentLiabilities",
                "ifrs-full_NoncurrentLiabilities", "ifrs-full_Liabilities",
                "ifrs-full_NetAssetsLiabilities", "ifrs-full_Equity")
    qualified = qualify(printed, context)
    tables = {id(line.table): line.table for line in qualified}
    witnesses = []
    for table in tables.values():
        if (not isinstance(table.money_scale, Decimal) or not table.money_scale.is_finite()
                or table.money_scale <= 0):
            continue
        supporting = tuple(tuple(row for row in table.rows if row.concept == name)
                           for name in concepts)
        if any(len(rows) != 1 for rows in supporting):
            continue
        rows = tuple(found[0] for found in supporting)
        if (any(row.table != table.position or row.measurement != "MONEY" for row in rows)
                or any(left.position >= right.position for left, right in zip(rows, rows[1:]))):
            continue
        lines_by_row = tuple(tuple(line for line in qualified
                                   if line.table is table and line.row is row) for row in rows)
        if any(len(lines) != 1 for lines in lines_by_row):
            continue
        for column in table.active:
            header = table.headers[column]
            if (header.column != column or header.end != context.end or header.currency != context.currency
                    or header.period_conflict or any(value is not None for value in
                        (header.start, header.duration_months, header.duration_weeks))
                    or not isinstance(header.money_scale, Decimal)
                    or not header.money_scale.is_finite() or header.money_scale <= 0):
                continue
            cells = tuple(row.cells[column] for row in rows)
            if any(column not in lines[0].positions
                   or lines[0][1][lines[0].positions.index(column)] is None for lines in lines_by_row):
                continue
            if any(lines[0].cells[lines[0].positions.index(column)] is not cell
                   or lines[0].headers[lines[0].positions.index(column)] is not header
                   for lines, cell in zip(lines_by_row, cells)):
                continue
            if any(cell.status != "NUMERIC" or cell.column_span != 1
                   or cell.column != column or cell.physical_column != cells[0].physical_column
                   or cell.physical_column != len(row.label_cells) + column
                   or cell.currency != context.currency or cell.multiplier != header.money_scale
                   or any(not isinstance(value, Decimal) or not value.is_finite()
                          for value in (cell.raw, cell.multiplier, cell.value))
                   or _number(cell.lexical)[0] != "NUMERIC"
                   or _number(cell.lexical)[1].as_tuple() != cell.raw.as_tuple()
                   for row, cell in zip(rows, cells)):
                continue
            try:
                with localcontext() as arithmetic:
                    for signal in (Inexact, Rounded, Overflow, Underflow, InvalidOperation):
                        arithmetic.traps[signal] = True
                    if any(cell.raw * cell.multiplier != cell.value for cell in cells):
                        continue
                    assets, current, noncurrent, total, net_assets, equity = (
                        cell.value for cell in cells)
                    if (assets <= 0 or any(value >= 0 for value in (current, noncurrent, total))
                            or current + noncurrent != total or assets + total != net_assets
                            or net_assets != equity):
                        continue
                    comparand = -current
            except DecimalException:
                continue
            witnesses.append(LiabilityDeductionWitness(
                "NET_ASSETS_DEDUCTION", "RENDERED_TABLE_LAYOUT", context, concept,
                table, rows[1], cells[1], header, comparand, rows,
                ("CurrentLiabilities + NoncurrentLiabilities = Liabilities",
                 "Assets + Liabilities = NetAssetsLiabilities", "NetAssetsLiabilities = Equity")))
    return witnesses[0] if len(witnesses) == 1 else None


def _label_key(label: str) -> str:
    """A printed label reduced to what it calls the line.

    Footnote markers and the renderer's note of the unit fall away: JPMorgan heads
    its earnings "Diluted earnings per share (in dollars per share)" and Apple heads
    the same line just "Diluted". Written once, because the two matchers below had
    a copy each and one of them drifted.
    """
    clean = re.sub(r"\s+", " ", label.lower()).strip(" .:")
    clean = re.sub(r"\s*\[\d+\]$", "", clean)
    return _UNIT_SUFFIX.sub("", clean).strip()


def value_for(statement_lines, *phrases: str, column: int = 0) -> tuple[str, Decimal] | None:
    """The first line whose label matches one of these phrases, longest first.

    Labels are matched whole-word-ish rather than by substring: "Total assets"
    must not match "Total assets of discontinued operations", and "Total
    liabilities" must not match "Total liabilities and equity", which is a
    different and much larger number.
    """
    for phrase in phrases:
        target = phrase.lower().rstrip("*")
        prefix = phrase.endswith("*")
        for label, values in statement_lines:
            clean = _label_key(label)
            if clean == target or (prefix and clean.startswith(target)
                                   and not _NOT_THE_PARENT.search(clean)):
                if column < len(values) and values[column] is not None:
                    return label, values[column]
    return None


def all_matching(statement_lines, *phrases: str, column: int = 0,
                 only_the_parent: bool = True,
                 money_only: bool = False) -> list[tuple[str, Decimal]]:
    """Every line whose label matches one of these phrases, in printed order.

    Where one phrase means two different figures depending on the filer, a single
    best match cannot be chosen by label. Edison calls the parent's share "Net
    income available to Edison International common shareholders" and prints the
    consolidated total as "Net income"; Uniti prints the parent's share AS "Net
    income" and uses "attributable to common shareholders" for the figure after
    preferred dividends. The same words, opposite meanings.

    So the caller asks a weaker but answerable question: is the figure on the panel
    one of the numbers this statement prints for this concept? A value that appears
    nowhere on the page is still caught; a value that appears is corroborated
    without the checker having to out-guess the filer's wording.
    """
    out = []
    for phrase in phrases:
        suffix, prefix = phrase.startswith("*"), phrase.endswith("*")
        target = phrase.lower().strip("*")
        for line in statement_lines:
            label, values = line
            clean = _label_key(label)
            if only_the_parent and _NOT_THE_PARENT.search(clean) and "common" not in clean:
                continue                        # the minority holders' own line
            # "Diluted (in shares)" and "Diluted (in dollars per share)" are the same
            # word once the unit suffix falls away, and one of them is a count of
            # shares. Where a per-share figure is wanted, the count is not it.
            if money_only and (_IN_SHARES.search(label) or
                               getattr(getattr(line, "row", None), "measurement", None) == "SHARES"):
                continue
            hit = (clean.endswith(target) if suffix else
                   clean.startswith(target) if prefix else clean == target)
            if hit and column < len(values) and values[column] is not None:
                out.append((label, values[column]))
    return out


# Every rendered row carries the concept it was tagged with, in the link that opens
# its definition: `defref_us-gaap_Assets`, `defref_aapl_IntangibleAssets...`.
_ELEMENT = re.compile(r"defref_([A-Za-z0-9-]+_[A-Za-z0-9]+)")


def elements(document: str | Statement) -> dict[str, list[Decimal | None]]:
    """Legacy concept view; conflicting repeated rows remain unresolvable.

    The physical result retains all rows, including their section and basis.
    This older dictionary cannot choose between ordinary and receipt rows.
    """
    return elements_from_lines(lines(document))


def elements_from_lines(statement_lines) -> dict[str, list[Decimal | None]]:
    """Keep the context of an already qualified numeric projection."""
    out = {}
    contexts = {}
    for line in statement_lines:
        row = line.row
        if not row.concept or not any(cell.raw is not None for cell in line.cells):
            continue
        values = line[1]
        context = (line.headers, row.measurement, row.basis)
        if row.concept not in out:
            out[row.concept], contexts[row.concept] = values, context
        elif contexts[row.concept] != context or out[row.concept] != values:
            out[row.concept] = [None] * max(len(out[row.concept]), len(values))
    return out
