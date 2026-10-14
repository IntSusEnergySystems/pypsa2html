"""The district-heating network as a year: supply, store, price groups, electric heat.

One small network whose every number can be computed by hand.  Ninety-six hours:

* hours 0-47, **surplus**: electricity costs 1 EUR/MWh, heat 2 EUR/MWh_th.  An
  electric boiler delivers 6 MW_th (4 to the users, 2 into the thermal store);
* hours 48-95, **scarcity**: electricity costs 120, heat 100.  A gas CHP delivers
  3 MW_th and the store gives back 1 MW_th.

The users take 4 MW_th throughout (3 buildings + 1 industry).  A heat pump is
available (COP 3, 100 000 EUR/MW_th a year) and not built, a resistive boiler is
built (10 MW_e, 9 000 EUR/MW_e a year).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest

from pypsa2html.charts import district_heating as chart
from pypsa2html.charts.base import combine_charts
from pypsa2html.extract.district_heating import (
    PRICE_GROUPS,
    band_labels,
    band_of,
    by_price_group,
    daily_mean,
    district_heat_year,
    electric_heat_value,
)
from pypsa2html.nodes import NodeResolver

HOURS = 96
SURPLUS = slice(0, 48)
SCARCE = slice(48, 96)
HEAT_PRICE = np.r_[np.full(48, 2.0), np.full(48, 100.0)]
ELEC_PRICE = np.r_[np.full(48, 1.0), np.full(48, 120.0)]


def _region(n: pypsa.Network, name: str, idx: pd.DatetimeIndex, *, with_store: bool = True):
    for suffix, carrier in (
        ("", "AC"), (" low voltage", "low voltage"), (" gas", "gas"),
        (" urban central heat", "urban central heat"),
        (" urban central water pits", "urban central water pits"),
    ):
        n.add("Bus", f"{name}{suffix}", carrier=carrier, location=name)
    heat, lv = f"{name} urban central heat", f"{name} low voltage"
    n.add("Load", f"{name} urban central heat", bus=heat, carrier="urban central heat")
    n.add("Load", f"{name} low-temperature heat for industry", bus=heat,
          carrier="low-temperature heat for industry")
    n.add("Link", f"{name} urban central gas CHP", bus0=f"{name} gas", bus1=name, bus2=heat,
          carrier="urban central gas CHP", efficiency=0.4, efficiency2=0.5)
    n.add("Link", f"{name} urban central resistive heater-2030", bus0=lv, bus1=heat,
          carrier="urban central resistive heater", efficiency=0.99, p_nom_extendable=True,
          p_nom_opt=10.0, capital_cost=9000.0, build_year=2030)
    n.add("Link", f"{name} urban central air heat pump-2030", bus0=heat, bus1=lv,
          carrier="urban central air heat pump", efficiency=1.0, p_nom_extendable=True,
          p_nom_opt=0.0, capital_cost=100000.0, build_year=2030, p_max_pu=0.0)
    if with_store:
        n.add("Store", f"{name} urban central water pits-2030", bus=f"{name} urban central water pits",
              carrier="urban central water pits", e_nom_extendable=True, e_nom_opt=100.0)
        n.add("Link", f"{name} urban central water pits charger-2030", bus0=heat,
              bus1=f"{name} urban central water pits", carrier="urban central water pits charger")
        n.add("Link", f"{name} urban central water pits discharger-2030",
              bus0=f"{name} urban central water pits", bus1=heat,
              carrier="urban central water pits discharger")


def _flows(n: pypsa.Network, idx: pd.DatetimeIndex, names: list[str], *, with_store: bool = True):
    zeros = pd.DataFrame(0.0, index=idx, columns=n.links.index)
    p0, p1, p2 = zeros.copy(), zeros.copy(), zeros.copy()
    eff = pd.DataFrame(1.0 / 3.0, index=idx, columns=[f"{r} urban central air heat pump-2030" for r in names])
    pmin = pd.DataFrame(-1.0, index=idx, columns=eff.columns)
    loads = {}
    soc, store_p = {}, {}
    for r in names:
        # surplus: boiler 6 MW_th out (6/0.99 MW_e in); scarcity: CHP 3 MW_th out
        p0.loc[idx[SURPLUS], f"{r} urban central resistive heater-2030"] = 6.0 / 0.99
        p1.loc[idx[SURPLUS], f"{r} urban central resistive heater-2030"] = -6.0
        p2.loc[idx[SCARCE], f"{r} urban central gas CHP"] = -3.0
        if with_store:
            p0.loc[idx[SURPLUS], f"{r} urban central water pits charger-2030"] = 2.0
            p1.loc[idx[SURPLUS], f"{r} urban central water pits charger-2030"] = -2.0
            p0.loc[idx[SCARCE], f"{r} urban central water pits discharger-2030"] = 1.0
            p1.loc[idx[SCARCE], f"{r} urban central water pits discharger-2030"] = -1.0
            level = np.r_[2.0 * np.arange(1, 49), 96.0 - np.arange(1, 49)]
            soc[f"{r} urban central water pits-2030"] = level
            store_p[f"{r} urban central water pits-2030"] = np.r_[np.full(48, -2.0), np.full(48, 1.0)]
        loads[f"{r} urban central heat"] = 3.0
        loads[f"{r} low-temperature heat for industry"] = 1.0
    n.links_t.p0, n.links_t.p1, n.links_t.p2 = p0, p1, p2
    n.links_t.efficiency = eff
    n.links_t.p_min_pu = pmin
    n.loads_t.p = pd.DataFrame({k: np.full(HOURS, v) for k, v in loads.items()}, index=idx)
    if with_store:
        n.stores_t.e = pd.DataFrame(soc, index=idx)
        n.stores_t.p = pd.DataFrame(store_p, index=idx)
    prices = {}
    for r in names:
        prices[f"{r} urban central heat"] = HEAT_PRICE
        prices[f"{r} low voltage"] = ELEC_PRICE
    n.buses_t.marginal_price = pd.DataFrame(prices, index=idx)


def _net(regions: tuple[str, ...] = ("AA",), *, with_store: bool = True) -> pypsa.Network:
    n = pypsa.Network()
    idx = pd.date_range("2030-01-01", periods=HOURS, freq="h")
    n.set_snapshots(idx)
    for r in regions:
        _region(n, r, idx, with_store=with_store)
    _flows(n, idx, list(regions), with_store=with_store)
    return n


def _ctx(n: pypsa.Network, groups: dict[str, list[str]] | None = None):
    groups = groups or {}
    return SimpleNamespace(
        networks={2030: n}, horizons=[2030],
        locations_for=lambda code: groups.get(code, [code]),
        resolver=lambda h: NodeResolver(n),
        _files={},
        config=SimpleNamespace(
            output=SimpleNamespace(plotly="cdn", chart_data=False),
            features=SimpleNamespace(price_abs_cap=1e4),
        ),
    )


@pytest.fixture
def ctx():
    return _ctx(_net())


SECTION = SimpleNamespace(title="District heating", unit="GW", id="dh")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_band_labels_and_classes():
    assert band_labels() == ["≤ 5", "5–20", "20–80", "> 80"]
    classes = band_of(pd.Series([0.0, 5.0, 5.01, 20.0, 80.0, 80.5, np.nan]))
    assert classes.iloc[:6].tolist() == [0, 0, 1, 1, 2, 3]
    assert np.isnan(classes.iloc[6])


def test_daily_mean_weights_snapshots_by_their_length():
    idx = pd.date_range("2030-01-01", periods=4, freq="6h")
    power = pd.Series([4.0, 4.0, 0.0, 0.0], index=idx)
    assert daily_mean(power, pd.Series(6.0, index=idx)).iloc[0] == pytest.approx(2.0)
    uneven = pd.Series([1.0, 3.0, 4.0, 4.0], index=idx)   # first snapshot stands for 1 h
    assert daily_mean(power, uneven).iloc[0] == pytest.approx((4 * 1 + 4 * 3) / 12)


# --------------------------------------------------------------------------
# the year
# --------------------------------------------------------------------------
def test_year_adds_up_supply_store_and_demand(ctx):
    dh = district_heat_year(ctx, "AA", 2030)
    energy = dh.producers.mul(dh.weights, axis=0).sum()
    assert set(dh.producers.columns) == {"Electric boiler", "Gas CHP"}
    assert energy["Electric boiler"] == pytest.approx(48 * 6.0)
    assert energy["Gas CHP"] == pytest.approx(48 * 3.0)
    assert dh.charging.sum() == pytest.approx(48 * 2.0)
    assert dh.discharging.sum() == pytest.approx(48 * 1.0)
    assert dh.demand.mul(dh.weights).sum() == pytest.approx(96 * 4.0)    # buildings + industry
    assert dh.annual_demand_twh == pytest.approx(384e-6)
    # supply + release = demand + filling, hour by hour
    gap = dh.producers.sum(axis=1) + dh.discharging - dh.demand - dh.charging
    assert gap.abs().max() == pytest.approx(0.0, abs=1e-9)


def test_year_reads_the_store_and_its_cycles(ctx):
    dh = district_heat_year(ctx, "AA", 2030)
    assert dh.soc_capacity == pytest.approx(100.0)
    assert dh.soc.max() == pytest.approx(96.0)
    # 48 MWh left the store over a 100 MWh capacity
    assert dh.store_cycles == pytest.approx(0.48)


def test_year_carries_the_two_prices_for_a_single_region(ctx):
    dh = district_heat_year(ctx, "AA", 2030)
    assert dh.single_region
    assert dh.heat_price.iloc[0] == 2.0 and dh.heat_price.iloc[-1] == 100.0
    assert dh.electricity_price.iloc[0] == 1.0 and dh.electricity_price.iloc[-1] == 120.0


def test_year_is_cached_and_none_without_a_heat_network(ctx):
    assert district_heat_year(ctx, "AA", 2030) is district_heat_year(ctx, "AA", 2030)
    ctx.locations_for = lambda code: ["ZZ"]
    assert district_heat_year(ctx, "ZZ", 2030) is None


def test_a_group_of_regions_sums_the_networks_and_drops_the_prices():
    n = _net(("AA", "BB"))
    ctx = _ctx(n, {"GRP": ["AA", "BB"]})
    dh = district_heat_year(ctx, "GRP", 2030)
    energy = dh.producers.mul(dh.weights, axis=0).sum()
    assert energy["Electric boiler"] == pytest.approx(2 * 48 * 6.0)
    assert not dh.single_region and dh.heat_price is None and dh.electricity_price is None
    assert dh.soc_capacity == pytest.approx(200.0)


def test_a_network_without_a_store_still_has_a_year():
    ctx = _ctx(_net(with_store=False))
    dh = district_heat_year(ctx, "AA", 2030)
    assert dh.soc is None and dh.soc_capacity is None and dh.store_cycles is None
    assert dh.charging.sum() == 0


# --------------------------------------------------------------------------
# energy by electricity price
# --------------------------------------------------------------------------
def test_price_groups_put_the_boiler_in_the_cheap_hours_and_the_chp_in_the_dear_ones(ctx):
    groups = by_price_group(ctx, "AA", 2030)
    assert len(groups.hours) == PRICE_GROUPS
    assert groups.hours.sum() == pytest.approx(96.0)
    assert groups.hours.max() - groups.hours.min() <= 1.0          # equal-length groups
    boiler, chp = groups.producers["Electric boiler"], groups.producers["Gas CHP"]
    assert boiler.iloc[:5].sum() == pytest.approx(0.288) and boiler.iloc[5:].sum() == 0
    assert chp.iloc[5:].sum() == pytest.approx(0.144) and chp.iloc[:5].sum() == 0
    # the whole year is accounted for
    assert groups.demand.sum() == pytest.approx(0.384)
    assert groups.charging.sum() == pytest.approx(0.096) and groups.discharging.sum() == pytest.approx(0.048)
    assert groups.electricity_price[0] == 1.0 and groups.electricity_price[-1] == 120.0
    assert groups.heat_price[0] == 2.0 and groups.heat_price[-1] == 100.0


def test_price_groups_say_what_electric_heat_costs_to_make(ctx):
    groups = by_price_group(ctx, "AA", 2030)
    # heat pump: 1/3 MWh of electricity per MWh_th; boiler: 1/0.99
    assert groups.cost_of_heat["Large air-source heat pump"][-1] == pytest.approx(120.0 / 3.0)
    assert groups.cost_of_heat["Electric boiler"][0] == pytest.approx(1.0 / 0.99)


def test_price_groups_need_one_region_with_prices():
    ctx = _ctx(_net(("AA", "BB")), {"GRP": ["AA", "BB"]})
    assert by_price_group(ctx, "GRP", 2030) is None


# --------------------------------------------------------------------------
# is electric heat worth building?
# --------------------------------------------------------------------------
def test_the_heat_pump_earns_a_fraction_of_its_cost_and_is_not_built(ctx):
    techs, info = electric_heat_value(ctx, "AA", 2030)
    pump = next(t for t in techs if t.carrier == "urban central air heat pump")
    # surplus: 2 - 1/3 * 1 = 5/3 a hour for 48 h; scarcity: 100 - 120/3 = 60 a hour for 48 h
    assert pump.margin_bands["≤ 5"] == pytest.approx(48 * 5.0 / 3.0)
    assert pump.margin_bands["> 80"] == pytest.approx(48 * 60.0)
    assert pump.margin_bands["5–20"] == 0 and pump.margin_bands["20–80"] == 0
    assert pump.margin == pytest.approx(80.0 + 2880.0)
    assert pump.capital_cost == pytest.approx(100000.0)
    assert pump.coverage == pytest.approx(2960.0 / 100000.0)
    assert pump.installed_mw_th == 0.0 and pump.heat_twh == 0.0 and pump.full_load_hours == 0.0
    assert pump.hours_positive == pytest.approx(96.0)
    assert info["labels"] == ["≤ 5", "5–20", "20–80", "> 80"]
    assert info["hours"].tolist() == [48.0, 0.0, 0.0, 48.0]


def test_the_boiler_is_rated_in_mw_th_not_mw_e(ctx):
    techs, _ = electric_heat_value(ctx, "AA", 2030)
    boiler = next(t for t in techs if t.carrier == "urban central resistive heater")
    assert boiler.installed_mw_th == pytest.approx(10.0 * 0.99)
    assert boiler.new_mw_th == pytest.approx(10.0 * 0.99)          # built in this horizon
    # per MW_e: 0.99 * 2 - 1 = 0.98 an hour in the surplus, nothing when electricity is dear
    assert boiler.margin == pytest.approx(48 * 0.98 / 0.99)
    assert boiler.capital_cost == pytest.approx(9000.0 / 0.99)
    assert boiler.heat_twh == pytest.approx(288e-6)
    assert boiler.full_load_hours == pytest.approx(288.0 / 9.9)
    assert boiler.electricity_price_paid == pytest.approx(1.0)      # it only ever bought cheap power
    assert boiler.cop == pytest.approx(0.99)


def test_a_technology_built_at_the_margin_earns_exactly_its_cost():
    """The resistive boiler is the check on the whole calculation.

    Give the prices the shape an optimiser would have produced -- the boiler is
    built until its last MW earns its capital cost -- and the test reads back
    a coverage of one.
    """
    n = _net()
    # capital cost = what one MW_th earns in the surplus: 48 * 0.98 / 0.99 per MW_th, times 0.99 per MW_e
    n.links.loc["AA urban central resistive heater-2030", "capital_cost"] = 48 * 0.98
    techs, _ = electric_heat_value(_ctx(n), "AA", 2030)
    boiler = next(t for t in techs if t.carrier == "urban central resistive heater")
    assert boiler.coverage == pytest.approx(1.0)


def test_electric_heat_is_for_a_single_region():
    ctx = _ctx(_net(("AA", "BB")), {"GRP": ["AA", "BB"]})
    assert electric_heat_value(ctx, "GRP", 2030) is None


def test_no_prices_no_verdict():
    n = _net()
    n.buses_t.marginal_price = pd.DataFrame(index=n.snapshots)
    assert electric_heat_value(_ctx(n), "AA", 2030) is None


# --------------------------------------------------------------------------
# the charts
# --------------------------------------------------------------------------
def test_year_view_draws_supply_store_and_prices(ctx):
    html = str(chart.year_view(ctx, "AA", SECTION))
    for text in ("Electric boiler", "Gas CHP", "Heat demand", "Thermal store: filling",
                 "Thermal store: releasing", "Energy in the thermal store", "Heat price"):
        assert text in html
    assert "How to read this" in html


def test_year_figure_stacks_supply_above_zero_and_the_filling_band_below(ctx):
    dh = district_heat_year(ctx, "AA", 2030)
    fig = chart._year_figure(dh, {}, True, True)
    by_name = {t.name: t for t in fig.data}
    assert by_name["Electric boiler"].stackgroup == "supply"
    assert by_name["Thermal store: releasing"].stackgroup == "supply"
    assert by_name["Thermal store: filling"].stackgroup == "filling"
    assert min(by_name["Thermal store: filling"].y) < 0 <= min(by_name["Thermal store: releasing"].y)
    # day one: the boiler delivers 6 MW_th = 0.006 GW
    assert by_name["Electric boiler"].y[0] == pytest.approx(0.006)
    # the demand line is the users' 4 MW_th, all year
    assert set(by_name["Heat demand"].y) == {0.004}


def test_every_horizon_of_the_year_chart_has_the_same_grid():
    """The dropdown swaps main axes, not the subplot grid: panels must not differ."""
    n = _net()
    ctx = _ctx(n)
    dh = district_heat_year(ctx, "AA", 2030)
    with_prices = chart._year_figure(dh, {}, True, True)
    without = chart._year_figure(dh, {}, True, True)
    assert {k for k in with_prices.layout if k.startswith("yaxis")} == \
        {k for k in without.layout if k.startswith("yaxis")}


def test_year_view_without_a_store_has_two_panels():
    ctx = _ctx(_net(with_store=False))
    fig = chart._year_figure(district_heat_year(ctx, "AA", 2030), {}, False, True)
    assert len([k for k in fig.layout if k.startswith("yaxis")]) == 2


def test_year_view_of_a_group_has_no_price_panel():
    ctx = _ctx(_net(("AA", "BB")), {"GRP": ["AA", "BB"]})
    html = str(chart.year_view(ctx, "GRP", SECTION))
    assert "Prices of the day" not in html and "Energy in the thermal store" in html


def test_storage_year_gives_capacity_cycles_and_the_table(ctx):
    html = str(chart.storage_year(ctx, "AA", SECTION))
    assert "0.5 full cycles a year" in html          # 48 MWh released over a 100 MWh store
    assert "Capacity (GWh)" in html and "Full cycles a year" in html
    assert "heating season" in html


def test_storage_year_is_one_panel_per_horizon_in_percent_of_capacity(ctx):
    n2 = _net()
    ctx.networks[2040] = n2
    ctx.horizons = [2030, 2040]
    html = str(chart.storage_year(ctx, "AA", SECTION))
    assert "2030: store of" in html and "2040: store of" in html
    dh = district_heat_year(ctx, "AA", 2030)
    pct = dh.soc.resample("D").mean() / dh.soc_capacity * 100.0
    assert pct.max() == pytest.approx(83.5)           # day 3: the store drains from 95 to 72 MWh of 100


def test_storage_year_is_absent_without_a_store():
    assert chart.storage_year(_ctx(_net(with_store=False)), "AA", SECTION) is None


def test_by_price_labels_name_the_open_ends(ctx):
    groups = by_price_group(ctx, "AA", 2030)
    labels = chart._price_labels(groups)
    assert labels[0] == "1" and labels[-1] == "120"              # 1 and 120 only: groups are flat
    spread = chart.PriceGroups(
        low=np.array([0.0, 2.0, 288.0]), high=np.array([2.0, 24.0, 1531.0]), hours=np.ones(3),
        producers=pd.DataFrame(), charging=np.zeros(3), discharging=np.zeros(3),
        demand=np.zeros(3), electricity_price=np.zeros(3), heat_price=None,
    )
    assert chart._price_labels(spread) == ["0–2", "2–24", "> 288"]
    html = str(chart.by_price(ctx, "AA", SECTION))
    assert "Heat price (right axis)" in html and "Electric boiler" in html


def test_by_price_is_absent_for_a_group_of_regions():
    ctx = _ctx(_net(("AA", "BB")), {"GRP": ["AA", "BB"]})
    assert chart.by_price(ctx, "GRP", SECTION) is None


def test_electric_heat_value_says_which_technology_is_not_built(ctx):
    html = str(chart.electric_heat_value(ctx, "AA", SECTION))
    assert "not built" in html
    assert "Large air-source heat pump" in html and "Electric boiler" in html
    assert "Annualised capital cost" in html and "Earnings / cost" in html


def test_electric_heat_figure_has_a_black_cost_marker_per_technology(ctx):
    techs, info = electric_heat_value(ctx, "AA", 2030)
    fig = chart._value_figure(techs, info)
    cost = next(t for t in fig.data if t.name == "Annualised capital cost")
    assert len(cost.x) == len(techs)
    # EUR/MW_th a year -> EUR/kW_th a year
    assert sorted(cost.y) == pytest.approx(sorted([100.0, 9000.0 / 0.99 / 1000.0]), abs=1e-3)
    assert fig.layout.barmode == "stack"


def test_nothing_without_a_heat_network():
    ctx = _ctx(_net())
    ctx.locations_for = lambda code: ["ZZ"]
    for builder in (chart.year_view, chart.storage_year, chart.by_price, chart.electric_heat_value):
        assert builder(ctx, "ZZ", SECTION) is None


# --------------------------------------------------------------------------
# the manifest and the dropdown
# --------------------------------------------------------------------------
def test_manifest_puts_the_four_sections_on_the_heat_page():
    from pypsa2html.pages import load_manifest

    heat = load_manifest()["heat"]
    sections = {s.id: s for s in heat.sections}
    assert list(sections)[:1] == ["heat_supply_mix"]
    assert {"dh_year", "dh_storage", "dh_by_price", "dh_electric_heat"} <= set(sections)
    # prices belong to one region: the two sections that read them skip group nodes
    assert sections["dh_by_price"].scope == "real" and sections["dh_electric_heat"].scope == "real"
    assert sections["dh_year"].scope == "all"


@pytest.mark.parametrize("builder", [
    "district_heating.year_view", "district_heating.storage_year",
    "district_heating.by_price", "district_heating.electric_heat_value",
])
def test_builders_resolve(builder):
    from pypsa2html.build import resolve_builder

    assert resolve_builder(builder) is not None


def _variant(value: float) -> "go.Figure":
    import plotly.graph_objects as go

    fig = go.Figure(go.Scatter(x=[1, 2], y=[value, value]))
    fig.update_layout(yaxis={"title": {"text": f"v{value}"}})
    return fig


def test_combine_charts_opens_on_the_initial_variant_and_the_menu_agrees():
    fig = combine_charts([("2025", _variant(1)), ("2030", _variant(2)), ("2050", _variant(3))],
                         menu_title="Horizon", initial="2050")
    assert [bool(t.visible) for t in fig.data] == [False, False, True]
    menu = fig.layout.updatemenus[0]
    assert menu.active == 2 and menu.buttons[2].label == "2050"
    assert fig.layout.yaxis.title.text == "v3"          # layout follows the variant on screen


def test_combine_charts_keeps_the_first_variant_by_default_and_ignores_unknown_labels():
    figures = [("2025", _variant(1)), ("2030", _variant(2))]
    for initial in (None, "1999"):
        fig = combine_charts(figures, initial=initial)
        assert [bool(t.visible) for t in fig.data] == [True, False]
        assert fig.layout.updatemenus[0].active == 0
