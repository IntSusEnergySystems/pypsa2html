"""Pure helpers in the extraction layer — no network required."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.extract.emissions import CARBON_FLOWS, _carrier_mask, _entry_names
from pypsa2html.extract.flows import (
    _aggregate_carriers,
    _assemble,
    _drop_structural,
    _group,
    _orient,
    _rank_entries,
)
from tests.conftest import requires_model


def test_rank_entries_is_stable_and_matches_legacy_suffix_rules():
    flows = pd.DataFrame(
        {
            "carrier": ["CCGT", "CCGT", "CCGT"],
            "source": ["gas", "gas", "gas"],
            "target": ["AC", "AC", "AC"],
            "value": [-1.0, 2.0, -3.0],
            "origin": ["link", "link", "link"],
            "port": [1, 1, 2],
        }
    )
    ranked = _rank_entries(_orient(flows))
    assert ranked.tolist() == ["CCGT", "CCGT_3", "CCGT_2"]


def test_orient_swaps_positive_link_flows_and_records_sign():
    flows = pd.DataFrame(
        {
            "carrier": ["pipe"],
            "source": ["gas"],
            "target": ["AC"],
            "value": [5.0],
            "origin": ["link"],
            "port": [1],
        }
    )
    oriented = _orient(flows)
    assert oriented.loc[0, "source"] == "AC"
    assert oriented.loc[0, "target"] == "gas"
    assert oriented.loc[0, "value"] == pytest.approx(5.0)
    assert oriented.loc[0, "sign"] == 1


def test_group_sums_rows_with_the_same_provenance_key():
    flows = pd.DataFrame(
        {
            "carrier": ["CCGT", "CCGT"],
            "source": ["gas", "gas"],
            "target": ["AC", "AC"],
            "value": [1.0, 2.0],
            "origin": ["link", "link"],
            "port": [1, 1],
            "sign": [0, 0],
        }
    )
    grouped = _group(flows)
    assert len(grouped) == 1
    assert grouped.loc[0, "value"] == pytest.approx(3.0)


def test_drop_structural_removes_self_loops_co2_and_duplicate_demands():
    flows = pd.DataFrame(
        {
            "carrier": ["x", "y", "z", "w"],
            "source": ["a", "co2 store", "gas for industry", "gas"],
            "target": ["a", "atm", "industry", "AC"],
            "value": [1.0, 2.0, 3.0, 4.0],
            "origin": ["link"] * 4,
            "port": [0] * 4,
        }
    )
    kept = _drop_structural(flows)
    assert list(kept["source"]) == ["gas"]
    assert kept.iloc[0]["value"] == pytest.approx(4.0)


def test_aggregate_carriers_collapses_low_voltage_and_ccgt_bus_carriers():
    flows = pd.DataFrame(
        {
            "carrier": ["gen", "link"],
            "source": ["low voltage node", "gas"],
            "target": ["CCGT unit", "AC"],
            "value": [1.0, 2.0],
            "origin": ["generator", "link"],
            "port": [0, 1],
        }
    )
    aggregated = _aggregate_carriers(flows)
    assert aggregated.loc[0, "source"] == "AC"
    assert aggregated.loc[1, "source"] == "gas"
    assert aggregated.loc[0, "target"] == "AC"


def test_assemble_threshold_filters_by_magnitude_not_sign():
    ctx = SimpleNamespace(config=SimpleNamespace(model=SimpleNamespace(flow_threshold=0.5)))
    table = pd.DataFrame(
        {
            "entry": ["a", "b"],
            "label": ["A", "B"],
            "unit": ["TWh", "TWh"],
            "code": ["code_a", "code_b"],
        }
    )
    values = {"2030": pd.Series({"a": -0.2, "b": 0.6})}
    out = _assemble(ctx, values, table)
    by_code = out.set_index("code")["2030"]
    assert by_code["code_a"] == pytest.approx(0.0)
    assert by_code["code_b"] == pytest.approx(0.6)


def test_carbon_entry_names_follow_table_order():
    names = _entry_names(CARBON_FLOWS[:6])
    assert names == [
        "DAC",
        "process emissions",
        "process emissions CC",
        "OCGT",
        "OCGT_2",
        "CCGT",
    ]


def test_carrier_mask_prefix_matches_cc_variants():
    static = pd.DataFrame({"carrier": ["SMR", "SMR CC", "OCGT"]}, index=["a", "b", "c"])
    flow = next(
        item for item in CARBON_FLOWS if item.label == "SMR" and item.match == "prefix"
    )
    mask = _carrier_mask(static, flow)
    assert list(static.index[mask]) == ["a", "b"]


@pytest.mark.needs_model
@requires_model("negawatt")
def test_energy_flows_reads_a_real_network():
    """End-to-end extraction against the négaWatt reference model."""
    from pypsa2html.config import load_config
    from pypsa2html.context import build_context
    from pypsa2html.extract.flows import energy_flows

    config_path = Path(__file__).resolve().parents[1] / "config" / "negawatt.yaml"
    ctx = build_context(load_config(config_path), "ref")
    table = energy_flows(ctx, "BE")
    assert {"code", "label", "unit"}.issubset(table.columns)
    assert set(ctx.year_columns).issubset(table.columns)
