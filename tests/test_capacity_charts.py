"""Faceted capacity charts mirror the legacy panel layout."""

from __future__ import annotations

import pandas as pd

from pypsa2html.charts.results import (
    _POWER_GROUPS,
    _POWER_GROUPS_AGGREGATE,
    _SOLAR_PV,
    _STORAGE_GROUPS,
    _faceted_capacity_chart,
    _fold_groups,
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
        index=["solar PV (rooftop)", "onshore wind", "offshore wind", "CCGT"],
    )
    fig = _faceted_capacity_chart(table, _POWER_GROUPS, unit="GW")
    assert fig is not None
    names = {t.name for t in fig.data}
    assert "Solar PV (rooftop)" in names and "Nuclear" in names and "CCGT" in names
    solar = next(t for t in fig.data if t.name == "Solar PV (rooftop)")
    assert list(solar.y) == [1.0, 2.0, 3.0, 4.0]
    assert list(solar.x) == years
    nuclear = next(t for t in fig.data if t.name == "Nuclear")
    assert list(nuclear.y) == [0.0, 0.0, 0.0, 0.0]


def test_pv_panel_carries_the_three_carriers_separately():
    """Ground / rooftop / tracking PV are three bars, not one.

    In the Walloon runs the split moves while the total barely does — ground
    PV goes to zero by 2050 and tracking PV appears — which a single "solar"
    bar hides.  See ``_SOLAR_PV`` in ``charts/results.py``.
    """
    table = pd.DataFrame(
        {"2025": [1770.0, 2318.0, 0.0], "2050": [5250.0, 0.0, 1305.0]},
        index=_SOLAR_PV,
    )
    fig = _faceted_capacity_chart(table, [_SOLAR_PV], unit="GW")
    assert fig is not None
    assert [t.name for t in fig.data] == [
        "Solar PV (rooftop)",
        "Solar PV (ground)",
        "Solar PV (tracking)",
    ]
    assert list(fig.data[1].y) == [2.318, 0.0]
    assert list(fig.data[2].y) == [0.0, 1.305]
    # The panel heading stays short: the members are named in the legend.
    assert fig.layout.annotations[0].text == "Solar PV"


def test_capacity_factor_panels_fold_pv_back_to_one_bar():
    """Three capacities, one capacity factor — see ``UTILISATION_FOLD``."""
    assert _fold_groups(_POWER_GROUPS)[0] == ["solar"]
    assert _fold_groups(_POWER_GROUPS_AGGREGATE)[0] == ["solar"]
    # Everything else is untouched.
    assert _fold_groups(_POWER_GROUPS)[1:] == _POWER_GROUPS[1:]


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


def test_pv_panel_stacks_while_every_other_panel_stays_grouped():
    """The three PV carriers add up to one fleet, so the capacity panel stacks.

    Plotly's ``barmode`` is figure-wide, so the per-panel choice rides on
    ``offsetgroup``: the PV traces share one, the rest each get their own.
    """
    from pypsa2html.charts.results import _STACKED_CAPACITY_PANELS

    table = pd.DataFrame(
        {"2025": [1770.0, 2318.0, 0.0, 3000.0, 500.0], "2050": [5250.0, 0.0, 1305.0, 4000.0, 900.0]},
        index=[*_SOLAR_PV, "onshore wind", "offshore wind"],
    )
    fig = _faceted_capacity_chart(
        table, _POWER_GROUPS, unit="GW", stacked_groups=_STACKED_CAPACITY_PANELS
    )
    assert fig.layout.barmode == "relative"
    by_name = {t.name: t for t in fig.data}
    pv = [by_name[n] for n in ("Solar PV (rooftop)", "Solar PV (ground)", "Solar PV (tracking)")]
    assert len({t.offsetgroup for t in pv}) == 1, "PV must share one slot to stack"
    wind = [by_name["Onshore wind"], by_name["Offshore wind"]]
    assert len({t.offsetgroup for t in wind}) == 2, "wind must keep separate slots"
    # The stack is the sum, and the values themselves are untouched.
    assert [sum(v) for v in zip(*(t.y for t in pv))] == [4.088, 6.555]


def test_capacity_factor_panels_are_never_stacked():
    """Capacity factors are intensive: stacking three would invent a number."""
    table = pd.DataFrame(
        {"2025": [0.11, 0.25, 0.4], "2050": [0.13, 0.26, 0.45]},
        index=["solar", "onshore wind", "offshore wind"],
    )
    fig = _faceted_capacity_chart(
        _fold_groups(_POWER_GROUPS) and table,
        _fold_groups(_POWER_GROUPS),
        unit="%",
        scale=100.0,
        shared_y=False,
        fill_missing=False,
    )
    assert fig.layout.barmode == "group"
    assert len({t.offsetgroup for t in fig.data}) == len(fig.data)
