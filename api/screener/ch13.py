"""Chapter-13 comparison statistics derived from the annual EPS series.

Graham compares smoothed three-year earning-power levels roughly five and ten
years apart, tests stability as the worst decline against the trailing three-year
average, and asks for positive earnings in every one of the last ten years.
Everything here is arithmetic over the audited annual series — no criterion, no
verdict. A statistic whose input years are missing is omitted, never guessed:
each result names the years it rests on so the reader can see the basis.
"""
from __future__ import annotations

from decimal import Decimal


def _avg3(eps: dict[int, Decimal], last: int) -> Decimal | None:
    """Average of the three fiscal years ending at `last`; None unless all exist."""
    ys = (last - 2, last - 1, last)
    if any(y not in eps for y in ys):
        return None
    return sum(eps[y] for y in ys) / 3


# Below this a three-year average is not an earnings level anyone can take a ratio
# against: Ultralife's FY2013-15 average is exactly zero and shipped a growth of
# 4.7e+32 %, Equinix's is 4.7 cents and shipped +23,164%.
_GROWTH_BASE_FLOOR = Decimal("0.05")


def _growth(recent: Decimal | None, earlier: Decimal | None) -> float | None:
    """Total % change between two smoothed levels — not CAGR. A non-positive base
    makes the percentage meaningless (the criterion-6 lesson), and so does a base
    too small to be an earnings level, so both give None."""
    if recent is None or earlier is None or earlier < _GROWTH_BASE_FLOOR:
        return None
    return round(float((recent / earlier - 1) * 100), 1)


# A record can reach the same place two ways, and Graham's preference between
# them is the whole of chapter 15's "reasonably" stable earnings: a business that
# grew through both halves of its record has been tested twice, while one whose
# decade went nowhere until the last three years is being priced on the part of
# its history that has not been tested at all.
_SPRINT_LATE = 50.0        # the recent block this far above the middle one
_SPRINT_EARLY = 10.0       # while the five years before it went nowhere, either way
_MARATHON_STEP = 10.0      # both halves rose at least this much
_MARATHON_DECLINE = 40.0   # and no single year gave back more than this


def _shape(early: float | None, late: float | None,
           unbroken: bool, worst: Decimal | None) -> str | None:
    """Which half of the record produced the growth, when the answer is clear.

    Only the two unambiguous shapes are named. Everything else — a record that
    fell, one that rose in the first half and stalled in the second, one too
    short to have two halves — is left unclassified rather than labelled by a
    threshold it barely crossed. A first half that *declined* is not a flat one:
    the recovery that follows a loss year is a rebound to where the company
    already was, and calling it new growth would misread the record.
    """
    if early is None or late is None:
        return None
    if late >= _SPRINT_LATE and abs(early) <= _SPRINT_EARLY:
        return "sprint"
    if (early >= _MARATHON_STEP and late >= _MARATHON_STEP and unbroken
            and worst is not None and worst <= _MARATHON_DECLINE):
        return "marathon"
    return None


def eps_stats(eps: dict[int, Decimal], *, latest_fy: int | None = None) -> dict | None:
    """Statistics anchored to the actual selected year, including unavailable EPS."""
    if not eps and latest_fy is None:
        return None
    last = latest_fy if latest_fy is not None else max(eps)
    avg_recent = _avg3(eps, last)
    avg_middle = _avg3(eps, last - 5)   # Graham's 1968-70 vs 1963-65 spacing
    avg_old = _avg3(eps, last - 10)     # ...vs 1958-60

    # stability: for each of the last ten years, the decline against the average
    # of the three preceding years; the worst one is the measure. A year is only
    # examined when all four inputs exist and the base average is positive.
    worst = None
    examined = []
    for t in range(last - 9, last + 1):
        base = _avg3(eps, t - 1)
        if t not in eps or base is None or base <= 0:
            continue
        examined.append(t)
        decline = max(Decimal(0), (base - eps[t]) / base * 100)
        if worst is None or decline > worst:
            worst = decline

    ten = [y for y in range(last - 9, last + 1) if y in eps]
    positive = [y for y in ten if eps[y] > 0]

    def f(v):
        return None if v is None else round(float(v), 2)

    # growth_10y adds the two halves of the record together and cannot say which
    # one produced the result; growth_5y is already the second half, so only the
    # first half is missing
    early = _growth(avg_middle, avg_old)
    late = _growth(avg_recent, avg_middle)
    return {
        "latest_fy": last,
        "avg_recent": f(avg_recent), "avg_middle": f(avg_middle), "avg_old": f(avg_old),
        "growth_5y": late,
        "growth_10y": _growth(avg_recent, avg_old),
        "growth_early": early,
        # the latest year against the three before it: a jump smoothing hides
        "latest_vs_prior3": _growth(eps.get(last), _avg3(eps, last - 1)),
        "shape": _shape(early, late, len(ten) == 10 and len(positive) == 10, worst),
        "max_decline": f(worst),
        "stability_years": len(examined),
        "ten_year_present": len(ten),
        "ten_year_positive": len(positive),
    }
