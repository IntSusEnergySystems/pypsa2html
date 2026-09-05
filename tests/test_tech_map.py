"""Technology grouping and colour tables (T5)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pypsa2html.charts.base import (
    apply_tech_map,
    normalize_carrier,
    omit_carriers,
    resolve_tech_color,
    tech_color_map,
)
from pypsa2html.datafiles import TECH_VIEWS, load_taxonomy, load_tech_groups

FIXTURE_DIR = Path(__file__).parent / "data" / "negawatt-ref" / "country_csvs"


@pytest.fixture(scope="module")
def tech_groups():
    return load_tech_groups()


def _fixture_techs(glob: str) -> list[str]:
    names: set[str] = set()
    for path in FIXTURE_DIR.glob(glob):
        df = pd.read_csv(path, index_col=0)
        names.update(str(x) for x in df.index)
    return sorted(names)


@pytest.mark.parametrize("view", ["costs", "capacities"])
def test_fixture_carriers_map_to_known_groups(tech_groups, view):
    glob = "*_costs.csv" if view == "costs" else "*_capacities.csv"
    groups = set(tech_groups.loc[tech_groups["view"] == view, "group"])
    for tech in _fixture_techs(glob):
        mapped = apply_tech_map(pd.Series([tech], index=[0]), view, tech_groups=tech_groups).iloc[0]
        assert mapped in groups or mapped == normalize_carrier(tech), (
            f"{tech!r} -> {mapped!r} is not a {view} group and did not pass through"
        )


def test_costs_heat_pump_maps_to_power_to_heat(tech_groups):
    raw = "services urban decentral air heat pump"
    assert normalize_carrier(raw) == "air heat pump"
    mapped = apply_tech_map(pd.Series([raw]), "costs", tech_groups=tech_groups).iloc[0]
    assert mapped == "power-to-heat"


def test_clustered_solar_maps_to_power_plants(tech_groups):
    mapped = apply_tech_map(pd.Series(["solar"]), "clustered", tech_groups=tech_groups).iloc[0]
    assert mapped == "Power Plants"


def test_gdp_bneur_present_for_be():
    tax = load_taxonomy()
    assert "gdp_bneur" in tax.regions.columns
    assert tax.regions.at["BE", "gdp_bneur"] == 568
    assert tax.regions.at["BEWAL", "gdp_bneur"] == 136
    assert tax.regions.at["LU", "gdp_bneur"] == 82


def test_tech_color_map_is_a_copy():
    a = tech_color_map()
    b = tech_color_map()
    assert a == b
    a["AC Transmission"] = "#000000"
    assert b["AC Transmission"] == "#ff3030"


def test_storage_display_names_have_distinct_colors():
    """Renamed storage labels must not collapse to the same grey fallback."""
    palette = tech_color_map()
    names = [
        "Thermal Energy Storage",
        "Grid-scale battery",
        "Gas storage",
        "EV battery",
        "H2 Store",
    ]
    colors = [resolve_tech_color(n, palette) for n in names]
    assert "lightgrey" not in colors
    assert len(set(colors)) == len(names)


def test_resolve_tech_color_case_and_unknown_fallback():
    palette = tech_color_map()
    assert resolve_tech_color("thermal energy storage", palette) == palette[
        "Thermal Energy Storage"
    ]
    unknown = resolve_tech_color("totally-unknown-tech-xyz", palette)
    assert unknown.startswith("#")
    assert unknown != "lightgrey"


def test_tech_views_are_complete(tech_groups):
    assert set(tech_groups["view"]) <= TECH_VIEWS


def test_power_to_gas_splits_into_named_series(tech_groups):
    """Item 14: Electrolysis / methanation / Fischer-Tropsch are separate groups."""
    for view in ("costs", "capacities", "dispatch"):
        assert apply_tech_map(pd.Series(["H2 Electrolysis"]), view, tech_groups=tech_groups).iloc[0] == "electrolysis"
        assert apply_tech_map(pd.Series(["methanation"]), view, tech_groups=tech_groups).iloc[0] == "methanation"
        assert apply_tech_map(pd.Series(["helmeth"]), view, tech_groups=tech_groups).iloc[0] == "methanation"
        assert apply_tech_map(pd.Series(["Fischer-Tropsch"]), view, tech_groups=tech_groups).iloc[0] == "Fischer-Tropsch"
        groups = set(tech_groups.loc[tech_groups["view"] == view, "group"])
        assert "power-to-gas" not in groups


def test_ccs_capacity_group_is_fuel_input_not_ccgt_cc(tech_groups):
    """Item 15: industry/SMR CC join CCS; CCGT CC stays a power-panel sibling.

    CHP CC is folded to ``CHP`` by ``normalize_carrier`` (any label containing
    "CHP"); putting it on the CCS stack would mix CHP MW with industry fuel-input
    MW — the mixed-unit bug items 5/7 already cost once.
    """
    mapped = apply_tech_map(
        pd.Series(
            [
                "solid biomass for industry CC",
                "gas for industry CC",
                "process emissions CC",
                "SMR CC",
                "CCGT CC",
                "urban central gas CHP CC",
            ]
        ),
        "capacities",
        tech_groups=tech_groups,
    )
    assert list(mapped.iloc[:4]) == ["CCS"] * 4
    assert mapped.iloc[4] == "CCGT CC"
    assert mapped.iloc[5] == "CHP"


def test_pv_carriers_get_one_capacity_group_each(tech_groups):
    """``solar`` / ``solar rooftop`` / ``solar-hsat`` are three capacity groups.

    They are different assets: ground-mounted and tracking PV compete for land
    and pay the ``electricity grid connection`` adder, rooftop does neither.
    Collapsing them hid the 2050 Walloon result — ground PV retiring to zero
    while tracking PV appears — behind a flat "solar" bar.
    """
    mapped = apply_tech_map(
        pd.Series(["solar", "solar rooftop", "solar-hsat"]),
        "capacities",
        tech_groups=tech_groups,
    )
    assert list(mapped) == [
        "solar PV (ground)",
        "solar PV (rooftop)",
        "solar PV (tracking)",
    ]


def test_solar_thermal_is_not_counted_as_pv_capacity(tech_groups):
    """Collectors are MW_th on a heat bus and must not stack onto the PV bar.

    The substring rule ``solar`` used to catch ``rural solar thermal`` and
    friends, which added 445 MW_th to BEWAL's 4 088 MW of 2025 PV — an 11 %
    overstatement on a chart whose axis says GW_e.
    """
    mapped = apply_tech_map(
        pd.Series(
            [
                "rural solar thermal",
                "urban central solar thermal",
                "urban decentral solar thermal",
            ]
        ),
        "capacities",
        tech_groups=tech_groups,
    )
    assert set(mapped) == {"solar thermal"}
