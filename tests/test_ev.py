"""Electric-vehicle extraction and charts, against ``tests/data/mini.nc``.

The fixture models the two locations differently on purpose (see
``tests/data/make_mini_network.py``): **AA** has the pypsa-wal natural/smart
split with V2G and a ``BEV charger`` link, **BB** has the upstream PyPSA-Eur
layout -- everything behind an ``EV charger`` link, no split, no V2G.  Every
test below that names AA or BB is pinning one of those two code paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import plotly.graph_objects as go
import pytest

from pypsa2html.build import resolve_builder
from pypsa2html.charts import ev as ev_charts
from pypsa2html.charts.base import combine_charts
from pypsa2html.config import DispatchWindowsConfig, ModelConfig
from pypsa2html.datafiles import load_tech_colors
from pypsa2html.extract.ev import (
    COUNTERFACTUAL,
    DELIVERED,
    MODES,
    NATURAL,
    NET,
    SMART,
    V2G,
    annual_energy,
    charging_profiles,
    ev_components,
)
from pypsa2html.nodes import NodeResolver
from pypsa2html.pages import load_manifest

MINI_NC = Path(__file__).parent / "data" / "mini.nc"

#: The fixture's only horizon, and a window covering all 24 of its snapshots.
HORIZON = 2030
WINDOW = ["2013-01-01 00:00", "2013-01-01 23:00"]


@pytest.fixture(scope="module")
def mini_network():
    pytest.importorskip("pypsa")
    if not MINI_NC.is_file():
        pytest.skip("tests/data/mini.nc missing — run make_mini_network.py")
    import pypsa

    return pypsa.Network(MINI_NC)


class _Networks(dict):
    """``NetworkCache`` stand-in: dict lookup plus ``first()``."""

    def first(self):
        return next(iter(self.values()))


@pytest.fixture
def ev_ctx(mini_network):
    """Build a :class:`BuildContext` stand-in restricted to one node."""
    resolver = NodeResolver(mini_network, "location")

    def _ctx(node: str):
        locations = None if node == "ALL" else [node]
        model = ModelConfig(
            dispatch_windows=DispatchWindowsConfig(winter=WINDOW, summer=WINDOW)
        )
        return SimpleNamespace(
            networks=_Networks({HORIZON: mini_network}),
            horizons=[HORIZON],
            resolver=lambda horizon: resolver,
            locations_for=lambda code: locations,
            is_aggregate=lambda code: code == "ALL",
            config=SimpleNamespace(
                model=model,
                output=SimpleNamespace(chart_data=False),
                project=SimpleNamespace(decimals=3),
            ),
            _files={},
        )

    return _ctx


def _section(section_id: str):
    return load_manifest()["dispatch"].sections[
        [s.id for s in load_manifest()["dispatch"].sections].index(section_id)
    ]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detects_the_split_fork_by_topology(mini_network):
    """AA's charger is named ``BEV charger``; nothing may match on that name."""
    resolver = NodeResolver(mini_network, "location")
    comp = ev_components(mini_network, resolver, ["AA"])
    assert list(comp.charge_links) == ["AA BEV charger"]
    assert list(comp.v2g_links) == ["AA V2G"]
    assert list(comp.natural_loads) == ["AA land transport EV inflexible"]
    assert list(comp.flexible_loads) == ["AA land transport EV"]
    assert list(comp.stores) == ["AA EV battery"]
    assert comp.has_split
    assert comp.has_v2g


def test_detects_the_upstream_layout_with_the_other_carrier_spelling(mini_network):
    """BB spells it ``EV charger`` (upstream / négaWatt) and has no split."""
    resolver = NodeResolver(mini_network, "location")
    comp = ev_components(mini_network, resolver, ["BB"])
    assert list(comp.charge_links) == ["BB EV charger"]
    assert list(comp.v2g_links) == []
    assert list(comp.natural_loads) == []
    assert list(comp.flexible_loads) == ["BB land transport EV"]
    assert comp
    assert not comp.has_split
    assert not comp.has_v2g


def test_aggregate_node_sums_both_fleets(mini_network):
    resolver = NodeResolver(mini_network, "location")
    comp = ev_components(mini_network, resolver, None)
    assert set(comp.charge_links) == {"AA BEV charger", "BB EV charger"}
    assert set(comp.buses) == {"AA EV battery", "BB EV battery"}


def test_no_electric_vehicles_is_falsy(mini_network):
    """A model without a fleet-battery bus must degrade, not raise."""
    pypsa = pytest.importorskip("pypsa")
    bare = pypsa.Network()
    bare.set_snapshots(pd.date_range("2013-01-01", periods=3, freq="h"))
    bare.add("Bus", "AA", carrier="AC", location="AA")
    comp = ev_components(bare, NodeResolver(bare, "location"), ["AA"])
    assert not comp
    assert not comp.has_split


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def test_grid_side_sign_convention(ev_ctx):
    data = charging_profiles(ev_ctx("AA"), "AA", HORIZON)
    assert (data.frame[NATURAL] >= 0).all()
    assert (data.frame[SMART] >= 0).all()
    assert (data.frame[V2G] <= 0).all()
    expected = data.frame[list(MODES)].sum(axis=1)
    pd.testing.assert_series_equal(data.frame[NET], expected, check_names=False)


def test_counterfactual_is_energy_neutral(ev_ctx):
    """The dashed curve must move energy in time, never create or destroy it."""
    for node in ("AA", "BB"):
        data = charging_profiles(ev_ctx(node), node, HORIZON)
        assert data.energy_mwh(COUNTERFACTUAL) == pytest.approx(
            data.energy_mwh(NET), rel=1e-9
        )


def test_counterfactual_follows_the_observed_profile_when_the_model_has_one(ev_ctx):
    data = charging_profiles(ev_ctx("AA"), "AA", HORIZON)
    assert data.shape_source == "observed profile"
    ratio = data.frame[COUNTERFACTUAL] / data.frame[NATURAL]
    assert ratio.std() == pytest.approx(0.0, abs=1e-9)
    # Uncontrolled charging carries the whole draw, so it is scaled up.
    assert ratio.iloc[0] > 1.0


def test_counterfactual_falls_back_to_the_no_flexibility_limit(ev_ctx):
    """Without a pinned load, the driving profile is what no-DSM would charge."""
    data = charging_profiles(ev_ctx("BB"), "BB", HORIZON)
    assert data.shape_source == "no-flexibility limit"
    assert (data.frame[NATURAL] == 0).all()
    driving = data.delivered
    ratio = data.frame[COUNTERFACTUAL] / driving
    assert ratio.std() == pytest.approx(0.0, abs=1e-9)


def test_delivered_is_below_the_grid_draw_because_of_charger_losses(ev_ctx):
    data = charging_profiles(ev_ctx("AA"), "AA", HORIZON)
    drawn = sum(data.energy_mwh(mode) for mode in MODES)
    assert 0 < data.delivered_mwh < drawn


def test_state_of_charge_is_a_percentage(ev_ctx):
    data = charging_profiles(ev_ctx("AA"), "AA", HORIZON)
    assert data.soc is not None
    assert data.soc.between(0, 100).all()


def test_window_restricts_the_frame(ev_ctx):
    ctx = ev_ctx("AA")
    full = charging_profiles(ctx, "AA", HORIZON)
    window = charging_profiles(
        ctx, "AA", HORIZON, start="2013-01-01 06:00", stop="2013-01-01 11:00"
    )
    assert len(full.frame) == 24
    assert len(window.frame) == 6
    assert len(window.weights) == 6
    # Recomputed for the window, not sliced from the annual counterfactual.
    assert window.energy_mwh(COUNTERFACTUAL) == pytest.approx(window.energy_mwh(NET))


def test_missing_horizon_returns_none(ev_ctx):
    assert charging_profiles(ev_ctx("AA"), "AA", 2099) is None


def test_context_without_networks_returns_none():
    ctx = SimpleNamespace(config=SimpleNamespace(model=ModelConfig()))
    assert charging_profiles(ctx, "AA", HORIZON) is None


def test_annual_energy_rows(ev_ctx):
    table = annual_energy(ev_ctx("AA"), "AA")
    assert list(table.columns) == [str(HORIZON)]
    assert set(table.index) == {NATURAL, SMART, V2G, DELIVERED}
    assert table.at[SMART, str(HORIZON)] > 0
    assert table.at[V2G, str(HORIZON)] < 0
    # Upstream layout: no natural-charging row at all, rather than a zero row.
    upstream = annual_energy(ev_ctx("BB"), "BB")
    assert NATURAL not in upstream.index
    assert V2G not in upstream.index


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "builder",
    ["ev.charging_winter", "ev.charging_summer", "ev.energy_by_mode"],
)
def test_ev_builders_resolve(builder):
    assert resolve_builder(builder) is not None


def test_manifest_puts_the_ev_sections_on_the_dispatch_page():
    section_ids = [s.id for s in load_manifest()["dispatch"].sections]
    assert section_ids[:4] == [
        "power_dispatch_winter",
        "power_dispatch_summer",
        "heat_dispatch_winter",
        "heat_dispatch_summer",
    ]
    assert "ev_charging_winter" in section_ids
    assert "ev_charging_summer" in section_ids
    assert "ev_energy_by_mode" in section_ids


def test_charging_chart_traces_and_caption(ev_ctx):
    fig = ev_charts.charging_winter(ev_ctx("AA"), "AA", _section("ev_charging_winter"))
    assert isinstance(fig, go.Figure)
    by_name = {str(trace.name): trace for trace in fig.data}
    assert NATURAL in by_name
    assert SMART in by_name
    assert V2G in by_name
    assert NET in by_name
    assert any(str(name).startswith(COUNTERFACTUAL) for name in by_name)
    assert by_name[V2G].stackgroup == "return"
    assert by_name[NATURAL].stackgroup == "draw"
    assert by_name[NET].stackgroup is None
    soc = next(t for t in fig.data if t.yaxis == "y2")
    assert fig.layout.yaxis2.range == (0, 105)
    assert soc.line.dash == "dot"
    caption = fig.layout.annotations[0].text
    assert "uncontrolled" in caption
    assert "V2G returned" in caption


def test_charging_chart_omits_the_net_line_without_v2g(ev_ctx):
    """Without V2G the stack top *is* the net draw; a second line would be noise."""
    fig = ev_charts.charging_winter(ev_ctx("BB"), "BB", _section("ev_charging_winter"))
    names = [str(trace.name) for trace in fig.data]
    assert NET not in names
    assert V2G not in names
    assert SMART in names


def test_energy_by_mode_marks_the_energy_reaching_the_vehicles(ev_ctx):
    fig = ev_charts.energy_by_mode(ev_ctx("AA"), "AA", _section("ev_energy_by_mode"))
    assert isinstance(fig, go.Figure)
    delivered = [t for t in fig.data if str(t.name) == DELIVERED]
    assert len(delivered) == 1
    assert delivered[0].type == "scatter"
    assert delivered[0].mode == "markers"
    assert [str(x) for x in delivered[0].x] == [str(HORIZON)]


def test_every_ev_series_has_a_palette_entry():
    """No EV series may fall through to the hashed fallback colour."""
    palette = set(load_tech_colors()["name"])
    for name in (*MODES, NET, COUNTERFACTUAL, DELIVERED, ev_charts._SOC_LABEL):
        assert name in palette


def test_translucent_fill_keeps_unparseable_colours():
    assert ev_charts._translucent("#7fa8c9", 0.5) == "rgba(127,168,201,0.5)"
    assert ev_charts._translucent("rgb(1,2,3)") == "rgb(1,2,3)"


# ---------------------------------------------------------------------------
# combine_charts: per-variant captions
# ---------------------------------------------------------------------------

def test_combine_charts_swaps_annotations_per_variant():
    """A caption is a layout property, so it must travel with its own button."""
    figures = []
    for horizon in (2030, 2040):
        fig = go.Figure(go.Scatter(x=[1], y=[1]))
        fig.add_annotation(text=f"peak in {horizon}", xref="paper", yref="paper")
        figures.append((str(horizon), fig))
    merged = combine_charts(figures, title="EV charging", menu_title="Horizon")
    buttons = merged.layout.updatemenus[0].buttons
    assert [b.args[1]["annotations"][0]["text"] for b in buttons] == [
        "peak in 2030",
        "peak in 2040",
    ]


def test_combine_charts_leaves_annotation_free_charts_alone():
    figures = [
        (label, go.Figure(go.Scatter(x=[1], y=[1]))) for label in ("a", "b")
    ]
    merged = combine_charts(figures)
    for button in merged.layout.updatemenus[0].buttons:
        assert "annotations" not in button.args[1]


def test_natural_charging_is_emitted_as_its_own_flow(mini_network, ev_ctx):
    """The inflexible EV load is copied as ``natural EV charging`` for the Sankey.

    AA has the split; BB does not, so the extra row is absent rather than zero.
    """
    from pypsa2html.extract.flows import _natural_charging_row

    aa = _natural_charging_row(ev_ctx("AA"), mini_network, "AA", HORIZON)
    assert list(aa["carrier"]) == ["natural EV charging"]
    assert float(aa["value"].iloc[0]) > 0

    bb = _natural_charging_row(ev_ctx("BB"), mini_network, "BB", HORIZON)
    assert bb.empty
