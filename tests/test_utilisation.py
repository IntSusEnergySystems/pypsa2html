"""Capacity factors and storage cycles: the utilisation view of the fleet.

The numbers are hand-checkable against ``tests/data/mini.nc``: the solar
generator runs a fixed profile against a fixed ``p_nom_opt``, and the CCGT
holds a constant 400 MW through a 2000 MW port.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from pypsa2html.charts.base import grouped_bar
from pypsa2html.charts.results import (
    _POWER_GROUPS,
    _POWER_GROUPS_AGGREGATE,
    _POWER_TO_FUEL,
    _faceted_capacity_chart,
    _fold_groups,
    _panel_title,
)
from pypsa2html.extract.tables import utilisation_table
from pypsa2html.nodes import NodeResolver

MINI_NC = Path(__file__).parent / "data" / "mini.nc"
N_SNAPSHOTS = 24


@pytest.fixture(scope="module")
def mini_network():
    pytest.importorskip("pypsa")
    if not MINI_NC.is_file():
        pytest.skip("tests/data/mini.nc missing — run make_mini_network.py")
    import pypsa

    return pypsa.Network(MINI_NC)


@pytest.fixture
def mini_ctx(mini_network):
    """A context over ``mini.nc`` with one horizon and no capacity filter."""
    resolver = NodeResolver(mini_network, "location")

    def component_index(horizon, component, node, *, bus_attr=None):
        static = getattr(mini_network, component)
        if node == "ALL":
            return static.index
        if component == "links" and bus_attr is None:
            assigned = resolver.first_real_link_nodes()
            return static.index[assigned == node]
        return resolver.select_locations(component, [node], bus_attr=bus_attr)

    return SimpleNamespace(
        networks={2030: mini_network},
        horizons=[2030],
        year_columns=["2030"],
        resolver=lambda horizon: resolver,
        component_index=component_index,
        locations_for=lambda node: None if node == "ALL" else [node],
        is_aggregate=lambda node: node == "ALL",
        is_study_wide=lambda node: node == "ALL",
        config=SimpleNamespace(
            raw={"features": {"capacity_filter": "off"}},
            features=SimpleNamespace(capacity_filter="off"),
            model=SimpleNamespace(base_year=None),
        ),
        _files={},
    )


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------

def test_solar_capacity_factor_matches_the_profile(mini_ctx):
    table = utilisation_table(mini_ctx, "AA", "power")
    assert table is not None
    profile = 0.5 + 0.5 * np.sin(np.linspace(0, 2 * np.pi, N_SNAPSHOTS))
    expected = profile.sum() * 600.0 / (800.0 * N_SNAPSHOTS)
    assert table.loc["solar", "2030"] == pytest.approx(expected)


def test_link_capacity_factor_uses_the_rated_port(mini_ctx):
    """The CCGT is rated at ``bus0`` (gas), so 400 of 2000 MW is 20 %.

    Dividing the *electrical* output by the same ``p_nom`` would give 10 % —
    an efficiency mistaken for an availability.
    """
    table = utilisation_table(mini_ctx, "AA", "power")
    assert table.loc["CCGT", "2030"] == pytest.approx(0.2)


def test_utilisation_respects_snapshot_weightings(mini_ctx, mini_network):
    """A 6h-resolution run must not report a sixth of the real factor."""
    before = utilisation_table(mini_ctx, "AA", "power").loc["CCGT", "2030"]
    mini_network.snapshot_weightings.loc[:, :] = 6.0
    mini_ctx._files.clear()
    after = utilisation_table(mini_ctx, "AA", "power").loc["CCGT", "2030"]
    mini_network.snapshot_weightings.loc[:, :] = 1.0
    # Energy and hours both scale by 6, so the factor is unchanged.
    assert after == pytest.approx(before)


def test_idle_storage_reports_zero_cycles(mini_ctx):
    """``AA battery store`` never discharges: 0 cycles, not a missing row."""
    table = utilisation_table(mini_ctx, "AA", "storage")
    assert table is not None
    assert table.loc["Grid-scale battery", "2030"] == pytest.approx(0.0)


def test_storage_cycles_count_only_discharge(mini_ctx, mini_network):
    """Charging is the other operating mode and must not be added in."""
    discharge = np.zeros(N_SNAPSHOTS)
    discharge[:12] = 200.0  # 12 h out at 200 MW = 2400 MWh
    discharge[12:] = -100.0  # and charging, which must be ignored
    mini_network.stores_t.p["AA battery store"] = discharge
    mini_ctx._files.clear()
    table = utilisation_table(mini_ctx, "AA", "storage")
    e_nom = float(mini_network.stores.at["AA battery store", "e_nom_opt"])
    assert table.loc["Grid-scale battery", "2030"] == pytest.approx(2400.0 / e_nom)
    mini_network.stores_t.p["AA battery store"] = 0.0


def test_no_capacity_means_no_factor(mini_ctx):
    """A factor is NaN, never 0, where nothing is installed — an absent plant
    is not an idle one."""
    table = utilisation_table(mini_ctx, "AA", "power")
    assert "nuclear" not in table.index  # nothing installed, no row at all
    assert table.notna().any(axis=1).all()


def test_factors_are_plausible_fractions(mini_ctx):
    table = utilisation_table(mini_ctx, "ALL", "power")
    values = table.stack().dropna()
    assert (values >= 0).all()
    assert (values <= 1.0 + 1e-9).all()


# ---------------------------------------------------------------------------
# The charts
# ---------------------------------------------------------------------------

def test_power_to_fuel_is_one_panel():
    assert _POWER_TO_FUEL in _POWER_GROUPS
    assert _POWER_TO_FUEL in _POWER_GROUPS_AGGREGATE
    for groups in (_POWER_GROUPS, _POWER_GROUPS_AGGREGATE):
        # None of the three still has a panel of its own.
        assert ["electrolysis"] not in groups
        assert ["methanation"] not in groups
        assert ["Fischer-Tropsch"] not in groups
    # the "(input-rated)" note is asserted in tests/test_heat_accounting.py
    assert _panel_title(_POWER_TO_FUEL).startswith("Power-to-fuel")


def test_power_to_fuel_panel_carries_all_three_bars():
    table = pd.DataFrame(
        {"2030": [0.4, 0.3, 0.9], "2040": [0.5, 0.2, 0.8]},
        index=["electrolysis", "methanation", "Fischer-Tropsch"],
    )
    fig = _faceted_capacity_chart(
        table, _POWER_GROUPS, unit="%", scale=100.0, fill_missing=False
    )
    assert fig is not None
    assert {t.name for t in fig.data} == {
        "Electrolysis",
        "Methanation",
        "Fischer-Tropsch",
    }
    electrolysis = next(t for t in fig.data if t.name == "Electrolysis")
    assert list(electrolysis.y) == pytest.approx([40.0, 50.0])


def test_fill_missing_false_invents_no_zero_bars():
    """A zero-height capacity bar says "none built"; a zero-height *factor*
    bar says "built and never run".  Only the first is safe to invent."""
    table = pd.DataFrame({"2030": [0.12]}, index=["solar"])
    # Utilisation panels use the folded groups, as ``capacity_factors_by_tech``
    # does: the capacity chart splits PV, the factor chart does not.
    groups = _fold_groups(_POWER_GROUPS)
    filled = _faceted_capacity_chart(table, groups, unit="%", scale=100.0)
    bare = _faceted_capacity_chart(
        table, groups, unit="%", scale=100.0, fill_missing=False
    )
    assert "Nuclear" in {t.name for t in filled.data}
    assert {t.name for t in bare.data} == {"Solar"}


def test_grouped_bar_puts_techs_on_x_and_years_in_the_legend():
    table = pd.DataFrame(
        {"2030": [0.25, 0.9], "2040": [0.27, float("nan")]},
        index=["onshore wind", "nuclear"],
    )
    fig = grouped_bar(table, unit="%", scale=100.0)
    assert fig is not None
    assert fig.layout.barmode == "group"
    assert [t.name for t in fig.data] == ["2030", "2040"]
    assert list(fig.data[0].x) == ["onshore wind", "nuclear"]
    assert list(fig.data[0].y) == pytest.approx([25.0, 90.0])
    # NaN stays a gap rather than becoming a zero bar.
    assert np.isnan(fig.data[1].y[1])


def test_grouped_bar_drops_all_missing_rows():
    table = pd.DataFrame(
        {"2030": [0.25, float("nan")], "2040": [0.27, float("nan")]},
        index=["onshore wind", "methanation"],
    )
    fig = grouped_bar(table, unit="%")
    assert list(fig.data[0].x) == ["onshore wind"]


def test_grouped_bar_returns_none_for_empty_table():
    assert grouped_bar(pd.DataFrame(), unit="%") is None
