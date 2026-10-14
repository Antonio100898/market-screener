import pytest
from pydantic import ValidationError

from screener.workspace_config import COLUMN_IDS, validate_config


def test_existing_unfiltered_view_and_columns_have_known_defaults():
    config = validate_config({})
    assert config["schema_revision"] == 1
    assert config["columns"] == list(COLUMN_IDS)
    assert config["view"] == {
        "q": "", "gaps": "ALL", "lens": "BOTH", "fit": "ALL", "profiles": [],
        "sectors": [], "venues": [], "indexes": [], "minCap": 0, "minMet": 0,
        "minPositiveEps": 0, "maxPe": 0, "maxPe3": 0, "trackedOnly": False,
        "hideNA": False, "hideNoApply": False, "belowNcav": False,
        "sort": [{"key": "fit", "dir": 1}],
    }


def test_existing_view_choices_preserve_values_and_sort_order():
    view = {"q": "ABT", "lens": "ENTERPRISING", "gaps": "ONE_GAP", "fit": "BLOCKED",
            "profiles": ["OPERATING"], "sectors": ["Health Care"], "venues": ["NYSE"],
            "indexes": ["S&P 500"], "minCap": 50e6, "minMet": 6, "minPositiveEps": 9,
            "maxPe": 30, "maxPe3": 25, "trackedOnly": True, "hideNA": True,
            "hideNoApply": True, "belowNcav": True,
            "sort": [{"key": "returns", "dir": -1}, {"key": "ticker", "dir": 1}]}
    assert validate_config({"view": view, "columns": ["ticker", "pe"]})["view"] == view


@pytest.mark.parametrize("configuration", [
    {"owner_id": 1}, {"formula": {"python": "import os"}}, {"provider_key": "secret"},
    {"schema_revision": 2}, {"schema_revision": True}, {"columns": ["sql"]},
    {"columns": ["pe", "pe"]}, {"view": {"maxPe": float("nan")}},
    {"view": {"maxPe": float("inf")}}, {"view": {"minCap": -1}},
    {"view": {"minMet": 7}}, {"view": {"minPositiveEps": 11}},
    {"view": {"trackedOnly": "false"}}, {"view": {"q": "x" * 1001}},
    {"view": {"sort": []}}, {"view": {"sort": [{"key": "sql", "dir": 1}]}},
    {"view": {"sort": [{"key": "pe", "dir": True}]}},
    {"view": {"sort": [{"key": "pe", "dir": 1}, {"key": "pe", "dir": -1}]}},
    {"view": {"sectors": ["Tech", "Tech"]}}, {"view": {"indexes": ["x"] * 101}},
    {"view": {"sectors": [str(i) + "界" * 110 for i in range(100)],
              "indexes": [str(i) + "界" * 110 for i in range(100)]}},
])
def test_unsupported_hostile_nonfinite_and_unbounded_config_is_rejected(configuration):
    with pytest.raises(ValidationError):
        validate_config(configuration)
