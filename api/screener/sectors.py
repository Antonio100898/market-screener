"""SIC code -> a sector a person would actually filter by.

SEC files companies under ~390 four-digit SIC codes, which is a filing taxonomy,
not an investing one. Its ten official divisions are the opposite problem:
"Manufacturing" swallows pharma, semiconductors, steel and aircraft alike.

These rules sit in between, roughly where GICS sectors sit. Ranges are checked in
order, so specific industries claim their codes before the broad ranges below.
"""
from __future__ import annotations

# (low, high, sector) — first match wins, so keep specific ranges above broad ones
RULES: tuple[tuple[int, int, str], ...] = (
    (6770, 6770, "Shell & blank-check"),          # SPACs: a large, distinctive bucket
    (2833, 2836, "Healthcare & pharma"),
    (3826, 3826, "Healthcare & pharma"),
    (3841, 3851, "Healthcare & pharma"),
    (8000, 8099, "Healthcare & pharma"),
    (3570, 3579, "Technology"),
    (3661, 3679, "Technology"),
    (7370, 7379, "Technology"),
    (6500, 6599, "Real estate"),
    (6798, 6798, "Real estate"),
    (6000, 6499, "Financials"),
    (6700, 6799, "Financials"),
    (1200, 1399, "Energy"),
    (2900, 2999, "Energy"),
    (4600, 4619, "Energy"),
    (4900, 4999, "Utilities"),
    (4800, 4899, "Communications & media"),
    (2700, 2799, "Communications & media"),
    (7310, 7319, "Communications & media"),
    (7800, 7999, "Communications & media"),
    (4000, 4599, "Transportation"),
    (4620, 4789, "Transportation"),
    (1000, 1099, "Materials & chemicals"),
    (1400, 1499, "Materials & chemicals"),
    (2600, 2699, "Materials & chemicals"),
    (2800, 2899, "Materials & chemicals"),          # pharma already claimed above
    (3200, 3399, "Materials & chemicals"),
    (2000, 2199, "Consumer staples"),
    (5140, 5149, "Consumer staples"),
    (5400, 5499, "Consumer staples"),
    (2200, 2399, "Consumer discretionary"),
    (2500, 2599, "Consumer discretionary"),
    (3000, 3199, "Consumer discretionary"),
    (3900, 3999, "Consumer discretionary"),
    (5200, 5999, "Consumer discretionary"),         # retail; grocery claimed above
    (7000, 7099, "Consumer discretionary"),
    (1500, 1799, "Industrials"),
    (3400, 3569, "Industrials"),
    (3580, 3660, "Industrials"),
    (3680, 3699, "Industrials"),
    (3700, 3799, "Industrials"),
    (3800, 3825, "Industrials"),
    (3827, 3840, "Industrials"),
    (3852, 3899, "Industrials"),
    (5000, 5199, "Wholesale & distribution"),
    (100, 999, "Agriculture"),
    (7200, 7299, "Business services"),
    (7320, 7369, "Business services"),
    (7380, 7399, "Business services"),
    (8100, 8999, "Business services"),
)

UNKNOWN = "Other"


def sector_for(sic: str | int | None) -> str | None:
    if sic in (None, ""):
        return None
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return None
    for low, high, name in RULES:
        if low <= code <= high:
            return name
    return UNKNOWN


def all_sectors() -> list[str]:
    return sorted({name for _, _, name in RULES} | {UNKNOWN})


# Tokyo Stock Exchange 33-industry classification -> the same sectors, placed
# where the matching SIC ranges above place their US peers.
JPX_SECTORS = {
    "Fishery, Agriculture and Forestry": "Agriculture",
    "Mining": "Materials & chemicals",
    "Construction": "Industrials",
    "Foods": "Consumer staples",
    "Textiles and Apparels": "Consumer discretionary",
    "Pulp and Paper": "Materials & chemicals",
    "Chemicals": "Materials & chemicals",
    "Pharmaceutical": "Healthcare & pharma",
    "Oil and Coal Products": "Energy",
    "Rubber Products": "Consumer discretionary",
    "Glass and Ceramics Products": "Materials & chemicals",
    "Iron and Steel": "Materials & chemicals",
    "Nonferrous Metals": "Materials & chemicals",
    "Metal Products": "Industrials",
    "Machinery": "Industrials",
    "Electric Appliances": "Technology",
    "Transportation Equipment": "Industrials",
    "Precision Instruments": "Industrials",
    "Other Products": "Consumer discretionary",
    "Electric Power and Gas": "Utilities",
    "Land Transportation": "Transportation",
    "Marine Transportation": "Transportation",
    "Air Transportation": "Transportation",
    "Warehousing and Harbor Transportation Service": "Transportation",
    "Information & Communication": "Technology",
    "Wholesale Trade": "Wholesale & distribution",
    "Retail Trade": "Consumer discretionary",
    "Banks": "Financials",
    "Securities and Commodities Futures": "Financials",
    "Insurance": "Financials",
    "Other Financing Business": "Financials",
    "Real Estate": "Real estate",
    "Services": "Business services",
}
