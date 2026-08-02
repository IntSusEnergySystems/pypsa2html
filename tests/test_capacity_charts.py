"""Faceted capacity charts mirror the legacy panel layout."""

from __future__ import annotations

import pandas as pd

from pypsa2html.charts.results import (
    _POWER_GROUPS,
    _STORAGE_GROUPS,
    _faceted_capacity_chart,
)


def test_faceted_power_capacity_fills_missing_techs_with_zeros():
    years = ["2025", "2030", "2040", "2050"]
    table = pd.DataFrame(
        {
            "2025": [1e3, 2e3, 0.0, 5e3],
            "2030": [2e3, 3e3, 1e3, 6e3],
            "2040": [3e3, 4e3, 2e3, 7e3],
            "2050": [4e3, 5e3, 3e3, 8e3],
        },
        index=["solar", "onshore wind", "offshore wind", "CCGT"],
    )
    fig = _faceted_capacity_chart(table, _POWER_GROUPS, unit="GW")
    assert fig is not None
    names = {t.name for t in fig.data}
    assert "Solar" in names and "Nuclear" in names and "CCGT" in names
    solar = next(t for t in fig.data if t.name == "Solar")
    assert list(solar.y) == [1.0, 2.0, 3.0, 4.0]
    assert list(solar.x) == years
    nuclear = next(t for t in fig.data if t.name == "Nuclear")
    assert list(nuclear.y) == [0.0, 0.0, 0.0, 0.0]


def test_faceted_storage_capacity_panels():
    table = pd.DataFrame(
        {"2030": [4e3, 1e5, 8e6], "2040": [5e3, 2e5, 8e6]},
        index=["Grid-scale battery", "Thermal Energy Storage", "Gas storage"],
    )
    fig = _faceted_capacity_chart(
        table, _STORAGE_GROUPS, unit="GWh", shared_y=False, height=500
    )
    assert fig is not None
    assert {t.name for t in fig.data} == {
        "Grid-scale battery",
        "Thermal Energy Storage",
        "Gas storage",
    }


def test_faceted_capacity_returns_none_for_empty_table():
    assert _faceted_capacity_chart(pd.DataFrame(), _POWER_GROUPS, unit="GW") is None
