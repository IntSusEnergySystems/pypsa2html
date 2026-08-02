"""Technology grouping and colour tables (T5)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pypsa2html.charts.base import (
    apply_tech_map,
    normalize_carrier,
    omit_carriers,
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


def test_tech_color_map_is_a_copy():
    a = tech_color_map()
    b = tech_color_map()
    assert a == b
    a["AC Transmission"] = "#000000"
    assert b["AC Transmission"] == "#ff3030"


def test_tech_views_are_complete(tech_groups):
    assert set(tech_groups["view"]) <= TECH_VIEWS


def test_capacity_omit_list_empty_by_default(tech_groups):
    """Capacity filtering is topology-based; tech_groups __omit__ is optional."""
    carriers = pd.Series(["biogas", "solar", "naphtha for industry"])
    assert not omit_carriers(carriers, "capacities", tech_groups=tech_groups).any()
