"""Capture-suffix convention: {tech} CC is a sibling, not a one-off."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.carriers import ccs_parent_carrier, fold_ccs_variants, inherit_ccs_entry
from pypsa2html.charts.base import apply_tech_map, resolve_tech_color, tech_color_map
from pypsa2html.charts.results import _POWER_GROUPS, _expand_capacity_group, _faceted_capacity_chart
from pypsa2html.datafiles import load_tech_groups
from pypsa2html.extract.emissions import _carbon_flow_covers, _discover_capture_flows
from pypsa2html.extract.flows import _assemble


def test_ccs_parent_and_entry_inheritance():
    assert ccs_parent_carrier("CCGT CC") == "CCGT"
    assert ccs_parent_carrier("coal CC") == "coal"
    assert ccs_parent_carrier("CCGT") is None
    assert inherit_ccs_entry("CCGT CC") == "CCGT"
    assert inherit_ccs_entry("CCGT CC_2") == "CCGT_2"
    assert inherit_ccs_entry("CCGT") is None


def test_fold_ccs_variants_adds_unmapped_child_onto_parent():
    series = pd.Series({"CCGT": 1.0, "CCGT CC": 0.4, "CCGT CC_2": 2.0, "CCGT_2": 0.1})
    folded = fold_ccs_variants(series, known_entries={"CCGT", "CCGT_2"})
    assert folded["CCGT"] == pytest.approx(1.4)
    assert folded["CCGT_2"] == pytest.approx(2.1)
    assert "CCGT CC" not in folded.index
    assert "CCGT CC_2" not in folded.index


def test_fold_ccs_variants_keeps_explicit_taxonomy_rows():
    """SMR CC is already in carrier_flows_*.csv — do not fold it onto SMR."""
    series = pd.Series({"SMR": 1.0, "SMR CC": 0.5, "SMR CC_2": 0.2})
    folded = fold_ccs_variants(series, known_entries={"SMR", "SMR CC", "SMR CC_2"})
    pd.testing.assert_series_equal(folded, series)


def test_assemble_inherits_ccgt_cc_energy_onto_gas_power():
    ctx = SimpleNamespace(config=SimpleNamespace(model=SimpleNamespace(flow_threshold=0.0)))
    table = pd.DataFrame(
        {
            "entry": ["CCGT", "CCGT_2"],
            "label": ["Gas-fired power", "losses"],
            "unit": ["TWh", "TWh"],
            "code": ["proelcgaz", "lossgas"],
        }
    )
    values = {"2030": pd.Series({"CCGT": 10.0, "CCGT CC": 3.0, "CCGT_2": 1.0, "CCGT CC_2": 0.5})}
    out = _assemble(ctx, values, table).set_index("code")
    assert out.loc["proelcgaz", "2030"] == pytest.approx(13.0)
    assert out.loc["lossgas", "2030"] == pytest.approx(1.5)


# PyPSA-Eur *names* the atmosphere bus ``co2 atmosphere`` but gives it the
# carrier ``co2``; the short spelling is what production networks actually have,
# so it is the one the parametrisation must lead with.  A fixture built only on
# the long spelling let _discover_capture_flows return () on every real network
# for three weeks without the suite noticing.
@pytest.mark.parametrize("atmosphere_carrier", ["co2", "co2 atmosphere"])
def test_discover_capture_finds_unlisted_four_bus_ccgt_cc(atmosphere_carrier):
    buses = pd.DataFrame(
        {
            "carrier": {
                "gas": "gas",
                "ac": "AC",
                "atm": atmosphere_carrier,
                "st": "co2 stored",
            }
        }
    )
    links = pd.DataFrame(
        {
            "bus0": ["gas", "gas", "gas"],
            "bus1": ["ac", "ac", "ac"],
            "bus2": ["atm", "atm", "atm"],
            "bus3": ["", "st", "st"],
            "carrier": ["CCGT", "CCGT CC", "SMR CC"],
        },
        index=["a", "b", "c"],
    )
    network = SimpleNamespace(buses=buses, links=links)
    found = _discover_capture_flows(network)
    assert [flow.carriers[0] for flow in found] == ["CCGT CC", "CCGT CC"]
    assert found[0].target == "co2 atmosphere" and found[0].port == 2
    assert found[1].target == "co2 stored" and found[1].port == 3
    assert found[0].source == "gas"
    assert _carbon_flow_covers("SMR CC")
    assert _carbon_flow_covers("CCGT")
    assert not _carbon_flow_covers("CCGT CC")


def test_costs_group_ccgt_cc_with_fossil_power():
    groups = load_tech_groups()
    mapped = apply_tech_map(pd.Series(["CCGT CC"]), "costs", tech_groups=groups).iloc[0]
    assert mapped == "Fossil fuels & powerplants"
    # Capacities view stays ungrouped so the plant appears as its own bar.
    cap = apply_tech_map(pd.Series(["CCGT CC"]), "capacities", tech_groups=groups).iloc[0]
    assert cap == "CCGT CC"


def test_ccgt_cc_has_its_own_colour_other_cc_uses_ccs_tint():
    palette = tech_color_map()
    assert resolve_tech_color("CCGT CC", palette) == palette["CCGT CC"]
    assert resolve_tech_color("CCGT CC", palette) != palette["CCGT"]
    assert resolve_tech_color("coal CC", palette) == palette["CCS"]


def test_faceted_capacity_panel_includes_ccs_sibling():
    table = pd.DataFrame(
        {"2030": [5e3, 1e3], "2040": [4e3, 2e3]},
        index=["CCGT", "CCGT CC"],
    )
    assert _expand_capacity_group(["CCGT"], table.index) == ["CCGT", "CCGT CC"]
    fig = _faceted_capacity_chart(table, _POWER_GROUPS, unit="GW")
    names = {t.name for t in fig.data}
    assert "CCGT" in names and "CCGT CC" in names
