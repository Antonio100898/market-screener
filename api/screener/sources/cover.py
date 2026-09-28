"""Layer 1: the cover page of a filing, which Company Facts cannot express.

Two facts on every cover decide what a price means, and neither survives the
Company Facts API — one because it is dimension-qualified, both because they are
text:

    dei:Security12bTitle   "American Depositary Shares, each representing 10
                            Ordinary Shares, par value $0.000006 per share"
    dei:TradingSymbol      "ZLAB"

They sit under a share-class axis, so the symbol is attached to *one* class. That
is the only deterministic answer to the question every per-share figure depends
on: which security does this ticker price? For a depositary receipt it also
carries the ratio between the receipt and the ordinary shares the statements
count — the number that makes a market capitalisation right or ten times too
large.

The rendered cover (SEC's R1 report) is read rather than the raw instance: it is
one small request per filing, carries the same tagged values, and needs no XBRL
toolchain.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

R_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/R{n}.htm"
# a cover is one of the first few rendered reports; beyond that come statements
COVER_REPORTS = (1, 2, 3)


def report_cache_path(
    cache_dir: str | Path, accession: str, report_number: int
) -> Path:
    """Exact bytes for one immutable rendered filing report."""
    return (
        Path(cache_dir)
        / "covers"
        / accession.replace("-", "")
        / f"R{report_number}.htm"
    )


def primary_document_cache_path(
    cache_dir: str | Path, accession: str, document_name: str
) -> Path:
    """Exact bytes for the filing's immutable primary document."""
    if not document_name or Path(document_name).name != document_name:
        raise ValueError("invalid SEC primary document name")
    return primary_document_cache_dir(cache_dir, accession) / document_name


def primary_document_cache_dir(cache_dir: str | Path, accession: str) -> Path:
    return (
        Path(cache_dir)
        / "covers"
        / accession.replace("-", "")
        / "primary"
    )


_INTEGER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_FRACTION_WORDS = {"half": 2, "quarter": 4, "fourth": 4, "tenth": 10}
_NUMBER_TOKEN = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|and|"
    r"million|half|quarter|fourth|tenth"
)
_NUMBER = (
    rf"(?:-?\d[\d,]*(?:\.\d+)?(?:\s*/\s*\d[\d,]*)?|"
    rf"\(\s*\d[\d,]*(?:\.\d+)?\s*\)|"
    rf"(?:{_NUMBER_TOKEN})(?:[\s-]+(?:{_NUMBER_TOKEN})){{0,7}})"
    rf"(?:\s*\(\s*\d[\d,]*(?:\.\d+)?\s*\))?"
)
_RECEIPT = r"(?:american\s+deposit(?:ary|ory)\s+(?:shares?|receipts?)|ADSs?|ADRs?)"
_SHARE = (
    r"(?:our\s+)?(?:(?:class|series)\s+[A-Z0-9-]+\s+)?"
    r"(?:(?-i:[A-Z][A-Za-z0-9&.'-]*)\s+){0,3}"
    r"(?:(?:common\s+ordinary|common|ordinary|equity)\s+shares?|[A-Z]\s+shares?|"
    r"shares?(?:\s+of\s+(?:common|ordinary)\s+stock)?|"
    r"(?:common|ordinary)\s+stock)"
)
_REPRESENT = r"represent(?:s|ed|ing)?"
_OF_ONE = r"(?:of\s+(?:one|a)\s+|a\s+)?"
_DIRECT_RELATIONS = (
    re.compile(
        rf"\b{_RECEIPT}[^.;]{{0,100}}?\bevery\s+(?P<receipts>{_NUMBER})\s*"
        rf"(?:of\s+which\s+)?{_REPRESENT}\s+(?P<shares>{_NUMBER})\s+"
        rf"(?:of\s+)?{_OF_ONE}{_SHARE}",
        re.I,
    ),
    re.compile(
        rf"(?<![\w-])(?:every\s+|each\s+)?(?P<receipts>{_NUMBER})\s*{_RECEIPT}\s*"
        rf"(?:,\s*)?{_REPRESENT}\s+(?P<shares>{_NUMBER})\s+(?:of\s+)?"
        rf"{_OF_ONE}{_SHARE}",
        re.I,
    ),
    re.compile(
        rf"\b(?:each|one|an?)\s+{_RECEIPT}\s+{_REPRESENT}\s+"
        rf"(?P<shares>{_NUMBER})\s+(?:of\s+)?{_OF_ONE}{_SHARE}",
        re.I,
    ),
    re.compile(
        rf"\b{_RECEIPT}[^.;]{{0,120}}?\beach\s+{_REPRESENT}\s+"
        rf"(?P<shares>{_NUMBER})\s+(?:of\s+)?{_OF_ONE}{_SHARE}",
        re.I,
    ),
    re.compile(
        rf"\beach\s+receipt\s+{_REPRESENT}\s+(?P<shares>{_NUMBER})\s+"
        rf"(?:of\s+)?{_OF_ONE}{_SHARE}",
        re.I,
    ),
)
_EQUAL_RELATION = re.compile(
    rf"\b{_RECEIPT}\s*,?\s*represent(?:s|ed|ing)?\s+an?\s+equal\s+number\s+of\s+{_SHARE}",
    re.I,
)
_COMPOUND_RELATION = re.compile(
    rf"\b{_RECEIPT}[^.;]{{0,80}}?\b{_REPRESENT}\s+{_NUMBER}\s+"
    r"(?:(?:BD|ordinary\s+participation)\s+)?"
    r"(?:units?|CPOs?|certificates?|(?:preferred|preference)\s+shares?)\b",
    re.I,
)
_DEPOSITARY_SECURITY = re.compile(
    r"\b(?:american\s+)?deposit(?:ary|ory)\b|\b(?:ADS|ADR)s?\b", re.I,
)
_NONCOMMON_SECURITY = re.compile(
    r"\b(?:preferred|preference|warrants?|rights?|notes?|bonds?|debentures?|debt|ETNs?)\b",
    re.I,
)
_ATTACHED_RIGHTS_SUFFIX = re.compile(
    r"(?:,\s*|\s*\(\s*|\s+)(?:including|together\s+with)\s+"
    r"(?:the\s+)?(?:associated\s*)?"
    r"(?:(?:common|ordinary|preferred)\s+)?(?:stock|share)\s+purchase\s+rights?\b"
    r"(?P<tail>.*)$",
    re.I,
)
_OTHER_NONCOMMON_SUFFIX = re.compile(
    r"\b(?:subscription|preferred|preference|warrants?|notes?|bonds?|debentures?|debt|ETNs?)\b",
    re.I,
)
_RECEIPT_RIGHT = re.compile(r"\bthe\s+right\s+to\s+receive\b", re.I)
_COMMON_SHARE_CLASS = re.compile(
    r"\b(?:common|ordinary)\s+(?:shares?|stock)\b|"
    r"\bshares?\s+of\s+(?:common|ordinary)\s+stock\b",
    re.I,
)
_COMMON_EQUITY = re.compile(
    r"\b(?:common|ordinary|voting)\b|\b(?:limited\s+partner|partnership\s+interest)\b|"
    r"\blimited\s+partnership\s+units?\b|"
    r"\btrust\s+units?\b|\bunits?\s+representing\b|"
    r"^(?:(?:class\s+)?[A-Z]\s+)?shares?\b", re.I,
)
_TITLE_LABEL = re.compile(
    r"^(?:Title of (?:12\(b\) Security|each class)|"
    r"Security\s*12\s*\(?b\)?(?:\s+Title)?|Security12bTitle)$", re.I,
)
_SYMBOL_LABEL = re.compile(r"^Trading Symbol(?:\(s\)|s)?$", re.I)
_PLACEHOLDER_SYMBOLS = {"", "true", "false", "none", "n/a", "not applicable"}


def text_of(document: str) -> str:
    """The rendered report as one line of readable text."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", document)))


def _table_securities(document: str) -> list[dict]:
    """Pair title and symbol values without flattening their table boundaries.

    SEC rendered covers do not use one stable row order.  Many put title before
    symbol, Toyota puts symbol before title, and OACC puts exchange between them.
    Flattening the whole report lets a title consume facts from the following
    share-class block.  At the cell level the label/value relationship is
    unambiguous, including covers that put several classes in parallel columns.
    """
    out: list[dict] = []
    for table in re.findall(r"<table\b[^>]*>(.*?)</table\s*>", document,
                            flags=re.I | re.S):
        titles: list[str] | None = None
        symbols: list[str] | None = None
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", table,
                              flags=re.I | re.S):
            cells = [text_of(cell).strip(" |\xa0") for cell in re.findall(
                r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row, flags=re.I | re.S
            )]
            if len(cells) < 2:
                continue
            label = cells[0]
            values = cells[1:]
            if _TITLE_LABEL.fullmatch(label):
                titles = values
            elif _SYMBOL_LABEL.fullmatch(label):
                symbols = [value if _valid_symbol(value) else "" for value in values]
            elif not label and titles and symbols is None and len(values) == len(titles):
                titles = [f"{title} {continuation}".strip() if continuation else title
                          for title, continuation in zip(titles, values)]
            if titles is not None and symbols is not None:
                if len(titles) == len(symbols):
                    out.extend({"title": title, "symbol": symbol}
                               for title, symbol in zip(titles, symbols)
                               if title and symbol)
                titles = None
                symbols = None
    return unique_securities(out)


def _valid_symbol(symbol: str) -> bool:
    return " ".join(symbol.split()).lower() not in _PLACEHOLDER_SYMBOLS


def symbol_matches(symbol: str, title: str, ticker: str) -> bool:
    """Whether a filed symbol cell names the exact priced ticker.

    SUZ files one ADS class as ``SUZB3/SUZ``. Both components are explicit in
    that cell. A debt symbol such as ``SUZ/29`` is not a compound ticker because
    its second component is only a maturity number.
    """
    if symbol == ticker:
        return True
    parts = [part.strip() for part in symbol.split("/")]
    return (len(parts) > 1 and is_common_equity_security(title)
            and all(re.fullmatch(r"[A-Z0-9.\-]+", part, re.I)
                    and re.search(r"[A-Z]", part, re.I) for part in parts)
            and ticker in parts)


def securities(document: str) -> list[dict]:
    """Every registered class the cover names, with the symbol attached to it.

    The rendered cover repeats one block per class, so title and symbol pair by
    position: the symbol that follows a title belongs to that title.
    """
    table_rows = _table_securities(document)
    if table_rows:
        return table_rows

    flat = text_of(document)
    out: list[dict] = []
    # Filers label the same two elements either way: "Title of 12(b) Security"
    # with "Trading Symbol", or "Title of each class" with "Trading Symbol(s)".
    # Onconova uses the second pair, and its title carries the 13:1 ratio that
    # nothing in Company Facts can express.
    # Exchange-rendered preferred symbols may contain spaces ("GLP pr B"). A
    # token-only capture truncated that to GLP, so the preferred row overwrote the
    # real GLP common-unit row in storage. The exchange-name cell is the reliable
    # right boundary; retain a narrow fallback below for older covers without it.
    block = re.compile(
        r"(?:Title of (?:12\(b\) Security|each class)|Security12bTitle)\s*(.+?)\s*"
        r"(?<!No )Trading Symbol\(?s?\)?\s+(?!Flag\b)(.+?)\s*"
        r"(?:Security Exchange Name|Name of each exchange)", re.I,
    )
    matches = list(block.finditer(flat))
    if not matches:
        # Some rendered 20-F covers expose the dimension member before the line
        # items and put TradingSymbol before Security12bTitle. Toyota's R1 is:
        # "American Depositary Shares [Member] ... Trading Symbol TM Title of
        # 12(b) Security American Depositary Shares Security Exchange Name NYSE".
        # Reading only the title-first layout misread the later boolean
        # "Trading Symbol Flag" as a ticker.
        symbol_first = re.compile(
            r"(?<!No )Trading Symbol(?:\(s\)|s)?\s+(?!Flag\b)([A-Z0-9.\-]{1,12})\s+"
            r"(?:Title of (?:12\(b\) Security|each class)|"
            r"Security\s*12\s*\(?b\)?(?:\s+Title)?|Security12bTitle)\s*"
            r"(.+?)\s+(?:Security Exchange Name|Name of each exchange)", re.I,
        )
        reversed_matches = list(symbol_first.finditer(flat))
        if reversed_matches:
            out = [
                {"title": match.group(2).strip(" |"),
                 "symbol": match.group(1).strip(" |")}
                for match in reversed_matches
                if 0 < len(match.group(2).strip(" |")) < 400
            ]
            if out:
                return unique_securities(out)
    matches = matches or list(re.finditer(
        r"(?:Title of (?:12\(b\) Security|each class)|"
        r"Security\s*12\s*\(?b\)?(?:\s+Title)?|Security12bTitle)\s*(.+?)\s*"
        r"(?<!No )Trading Symbol(?:\(s\)|s)?\s+(?!Flag\b)([A-Z0-9.\-]{1,12})",
        flat, re.I
    ))
    for match in matches:
        title = match.group(1).strip(" |")
        symbol = " ".join(match.group(2).strip(" |").split())
        if title and len(title) < 400 and len(symbol) < 40 and _valid_symbol(symbol):
            out.append({"title": title, "symbol": symbol})
    if out:
        return unique_securities(out)
    # A filer can tag the symbol and no class title at all — American Vanguard and
    # Manitowoc both do. There is nothing further to learn from their cover, and
    # saying so is worth more than leaving the question open forever.
    bare = re.search(
        r"(?<!No )Trading Symbol(?:\(s\)|s)?\s+(?!Flag\b)([A-Z0-9.\-]{1,12})",
        flat, re.I
    )
    if (bare and _valid_symbol(bare.group(1))
            and ("Cover" in flat or "Document Type" in flat)):
        return [{"title": "", "symbol": bare.group(1)}]
    return out


def unique_securities(rows: list[dict]) -> list[dict]:
    """One registered class per exact trading symbol.

    A 20-F can repeat the ADS ticker on the unlisted underlying ordinary-share
    row (LX), while older parsers could also truncate an exchange-rendered debt
    or preferred symbol to the common's plain ticker (HON/PPG/GLP). Storage is
    keyed by ``(cik, symbol)``, so letting the last row win silently changes what
    one priced share means. Prefer the depositary class, then common equity, and
    keep the first row when two descriptions have the same standing.
    """
    winners: dict[str, tuple[int, int, dict]] = {}
    for position, row in enumerate(rows):
        symbol = row.get("symbol") or ""
        title = row.get("title") or ""
        rank = 2 if is_depositary_security(title) else (1 if is_common_equity_security(title) else 0)
        current = winners.get(symbol)
        if current is None or rank > current[0]:
            winners[symbol] = (rank, position, row)
    return [winner[2] for winner in sorted(winners.values(), key=lambda item: item[1])]


def _integer_words(text: str) -> int | None:
    total = current = 0
    tokens = [token for token in text.replace("-", " ").split() if token != "and"]
    if not tokens:
        return None
    for token in tokens:
        if token in _INTEGER_WORDS:
            current += _INTEGER_WORDS[token]
        elif token == "hundred" and current:
            current *= 100
        elif token == "thousand" and current:
            total += current * 1000
            current = 0
        elif token == "million" and current:
            total += current * 1_000_000
            current = 0
        else:
            return None
    return total + current


def _number(text: str) -> Fraction | None:
    value = " ".join(text.strip().lower().split())
    annotation = re.fullmatch(r"(.*?)\s*\(\s*([\d,.]+)\s*\)", value)
    if annotation:
        written = _number(annotation.group(1)) if annotation.group(1) else None
        numeric = _number(annotation.group(2))
        return numeric if written is None else (written if written == numeric else None)
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    scaled = re.fullmatch(r"(.+?)\s+(thousand|million)", value)
    if scaled and re.search(r"\d", scaled.group(1)):
        base = _number(scaled.group(1))
        if base is not None:
            return base * (1000 if scaled.group(2) == "thousand" else 1_000_000)
    compact = value.replace(",", "").replace(" ", "")
    try:
        if "/" in compact:
            numerator, denominator = compact.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        if re.fullmatch(r"-?\d+(?:\.\d+)?", compact):
            return Fraction(Decimal(compact))
    except (ArithmeticError, ValueError):
        return None

    words = value.replace("-", " ")
    mixed = re.fullmatch(
        r"(.+?)\s+and\s+(?:one|a)\s+(half|quarter|fourth|tenth)", words
    )
    if mixed:
        whole = _integer_words(mixed.group(1))
        return (
            Fraction(whole, 1) + Fraction(1, _FRACTION_WORDS[mixed.group(2)])
            if whole is not None
            else None
        )
    fraction = re.fullmatch(
        r"(?:(one|a)\s+)?(half|quarter|fourth|tenth)(?:\s+a)?", words
    )
    if fraction:
        return Fraction(1, _FRACTION_WORDS[fraction.group(2)])
    integer = _integer_words(words)
    return Fraction(integer, 1) if integer is not None else None


def depositary_ratio(title: str) -> Decimal | None:
    """One unique direct underlying-share relation from filing text."""
    if not _DEPOSITARY_SECURITY.search(title or ""):
        return None
    if _COMPOUND_RELATION.search(title):
        return None

    ratios: set[Fraction] = set()
    invalid = False
    for pattern in _DIRECT_RELATIONS:
        for match in pattern.finditer(title):
            receipts = _number(match.groupdict().get("receipts") or "1")
            shares = _number(match.group("shares"))
            if receipts is None or shares is None or receipts <= 0 or shares <= 0:
                invalid = True
            else:
                ratios.add(shares / receipts)
    if _EQUAL_RELATION.search(title):
        ratios.add(Fraction(1, 1))
    if invalid or len(ratios) != 1:
        return None
    ratio = ratios.pop()
    return Decimal(ratio.numerator) / Decimal(ratio.denominator)


def is_depositary_security(title: str) -> bool:
    """Whether a registered-class title says the ticker prices a receipt.

    A foreign ordinary/common share needs no conversion. A depositary title does:
    without its underlying-shares-per-receipt ratio, prices and statement figures
    cannot safely be put on one basis.
    """
    return bool(_DEPOSITARY_SECURITY.search(title or ""))


def is_common_equity_security(title: str) -> bool:
    """Whether the registered class is an equity security this screen can price.

    Exact symbol matching can still land on a preferred share, warrant or ETN.
    Those securities do not own the common earnings/book value being screened.
    """
    title = title or ""
    primary_class = _RECEIPT_RIGHT.sub("", title)
    attached_rights = _ATTACHED_RIGHTS_SUFFIX.search(primary_class)
    if (attached_rights
            and _COMMON_SHARE_CLASS.search(primary_class, 0, attached_rights.start())
            and not _OTHER_NONCOMMON_SUFFIX.search(attached_rights.group("tail"))):
        primary_class = primary_class[:attached_rights.start()]
    if _NONCOMMON_SECURITY.search(primary_class):
        return False
    return is_depositary_security(title) or bool(_COMMON_EQUITY.search(title))


def is_untraded_underlying(title: str) -> bool:
    """Whether a starred 20-F row names the ordinary shares behind an ADS.

    Foreign covers commonly repeat the ADS symbol on an ordinary-share row and
    mark that row with ``*`` to say the ordinary shares are not themselves
    listed. Such a row cannot establish the priced security's identity.
    """
    title = title or ""
    return (title.rstrip().endswith("*") and not is_depositary_security(title)
            and is_common_equity_security(title))
