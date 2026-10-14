import json
import math

import pytest
from sqlalchemy import create_engine

from screener.company_projection import project_companies
from screener.display_publication import DisplayReleasePreparer, canonical_bytes, typed_projection


def projected_row():
    row = dict(ticker="ABT", cik="0000001800", price=10, shares=100,
               criteria=[{"n": 1, "status": "PASS", "value": 5}], n_pass=1,
               graham_profile="OPERATING", alignment={"enterprising": {"total": 6, "passed": 5}})
    return {**row, **project_companies([row])[0]}


def test_typed_columns_use_projected_values_and_criteria_numbers():
    row = projected_row()
    values = typed_projection(row, ordinal=7, security_id=1, input_bundle_sha256="a" * 64)
    assert (values["market_cap"], values["pe"], values["criteria_passed"], values["enterprising_gap"]) == (1000, 5, 1, "ONE_GAP")
    assert values["assumption_status"] == "UNAVAILABLE"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "10", True])
def test_nonfinite_or_nonnumber_query_value_is_rejected(bad):
    row = projected_row()
    row["mcap"] = bad
    with pytest.raises(ValueError, match="display field"):
        typed_projection(row, ordinal=0, security_id=1, input_bundle_sha256="a" * 64)


def test_count_cannot_silently_disagree_with_strict_criteria():
    row = projected_row()
    row["n_pass"] = 6
    with pytest.raises(ValueError, match="criteria count"):
        typed_projection(row, ordinal=0, security_id=1, input_bundle_sha256="a" * 64)


def test_canonical_input_bytes_are_stable_and_reject_nonfinite_values():
    assert canonical_bytes({"b": 2, "a": 1}) == canonical_bytes({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_bytes({"price": math.nan})


@pytest.mark.parametrize("mode,limitations", [("unknown", ["test"]), ("regression_import", []), ("caller_captured_enrichment", [""])])
def test_capture_mode_and_financial_gate_limits_are_explicit(mode, limitations):
    preparer = DisplayReleasePreparer(create_engine("sqlite://"), None)
    with pytest.raises(ValueError):
        preparer.prepare([], capture_mode=mode, limitations=limitations)
