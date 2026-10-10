"""Heat supply of buildings (pie of pies) and the district-heating dispatch.

The split must sum *exactly* to the residential/tertiary heat load: other users
of a heat bus (agriculture, industrial low-temperature heat) take their share
of every producer, and storage only moves heat in time, so neither appears in
the pies.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

from pypsa2html.charts import heat as heat_chart
from pypsa2html.extract.heat import (
    district_heat_label,
    heat_label,
    heat_split,
)
from pypsa2html.nodes import NodeResolver


def _net() -> pypsa.Network:
    n = pypsa.Network()
    idx = pd.date_range("2025-01-01", periods=2, freq="h")
    n.set_snapshots(idx)
    for name, carrier in (("AA", "AC"), ("AA gas", "gas"), ("AA rural heat", "rural heat"),
                          ("AA urban central heat", "urban central heat"),
                          ("AA urban central water pits", "urban central water pits")):
        n.add("Bus", name, carrier=carrier, location="AA")
    # rural bus: 8 MWh/h of buildings + 2 of agriculture; boiler 6, heat pump 4
    n.add("Load", "AA rural heat", bus="AA rural heat", carrier="rural heat")
    n.add("Load", "AA agriculture heat", bus="AA rural heat", carrier="agriculture heat")
    n.add("Link", "AA rural gas boiler", bus0="AA gas", bus1="AA rural heat",
          carrier="rural gas boiler")
    n.add("Link", "AA rural ground heat pump", bus0="AA rural heat", bus1="AA",
          carrier="rural ground heat pump")
    # district heating: 3 buildings + 1 industry; CHP 4.2, electric boiler 0.5,
    # and a pit storage charged at t0 and discharged at t1
    n.add("Load", "AA urban central heat", bus="AA urban central heat",
          carrier="urban central heat")
    n.add("Load", "AA low-temperature heat for industry", bus="AA urban central heat",
          carrier="low-temperature heat for industry")
    n.add("Link", "AA urban central gas CHP CC", bus0="AA gas", bus1="AA",
          bus2="AA urban central heat", carrier="urban central gas CHP CC")
    n.add("Link", "AA urban central resistive heater", bus0="AA", bus1="AA urban central heat",
          carrier="urban central resistive heater")
    n.add("Link", "AA urban central water pits charger", bus0="AA urban central heat",
          bus1="AA urban central water pits", carrier="urban central water pits charger")
    n.add("Link", "AA urban central water pits discharger", bus0="AA urban central water pits",
          bus1="AA urban central heat", carrier="urban central water pits discharger")

    n.loads_t.p = pd.DataFrame({
        "AA rural heat": [8.0, 8.0], "AA agriculture heat": [2.0, 2.0],
        "AA urban central heat": [3.0, 3.0], "AA low-temperature heat for industry": [1.0, 1.0],
    }, index=idx)
    zeros = pd.DataFrame(0.0, index=idx, columns=n.links.index)
    p0, p1, p2 = zeros.copy(), zeros.copy(), zeros.copy()
    p1["AA rural gas boiler"] = -6.0
    p0["AA rural ground heat pump"] = -4.0           # reversed link: heat out at bus0
    p2["AA urban central gas CHP CC"] = -4.2
    p1["AA urban central resistive heater"] = -0.5
    p0["AA urban central water pits charger"] = [1.0, 0.0]
    p1["AA urban central water pits discharger"] = [0.0, -0.8]
    n.links_t.p0, n.links_t.p1, n.links_t.p2 = p0, p1, p2
    return n


@pytest.fixture
def ctx():
    n = _net()
    return SimpleNamespace(
        networks={2025: n},
        horizons=[2025],
        locations_for=lambda code: [code],
        resolver=lambda h: NodeResolver(n),
        _files={},
        config=SimpleNamespace(output=SimpleNamespace(plotly="cdn", chart_data=False)),
    )


@pytest.mark.parametrize(("carrier", "district", "label"), [
    ("rural ground heat pump", False, "Ground-source heat pump"),
    ("urban decentral air heat pump", False, "Air-source heat pump"),
    ("urban central air heat pump", True, "Large air-source heat pump"),
    ("urban central gas CHP CC", True, "Gas CHP with capture"),
    ("urban central gas CHP", True, "Gas CHP"),
    ("urban central solid biomass CHP CC", True, "Biomass CHP with capture"),
    ("urban central resistive heater", True, "Electric boiler"),
    ("rural resistive heater", False, "Electric resistive heater"),
    ("residential rural oil boiler", False, "Oil boiler"),
    ("urban central gas boiler", True, "Gas boiler (district)"),
    ("Fischer-Tropsch", True, "Power-to-X waste heat"),
])
def test_heat_labels(carrier, district, label):
    assert heat_label(carrier, district) == label


def test_discharger_is_not_labelled_as_charging():
    assert district_heat_label("urban central water pits discharger") == "Thermal storage: discharging"
    assert district_heat_label("urban central water pits charger") == "Thermal storage: charging"
    assert district_heat_label("urban central heat") == "Heat demand (buildings)"
    assert district_heat_label("urban central gas CHP") == "Gas CHP"


def test_split_sums_to_the_building_load(ctx):
    split = heat_split(ctx, "AA", 2025)
    # 2 snapshots x (8 rural + 3 district) MWh = 22 MWh = 2.2e-5 TWh
    assert split.total == pytest.approx(22e-6)
    assert split.district == pytest.approx(6e-6)
    # rural: 16 MWh to buildings out of 20 produced -> boiler 60 %, HP 40 %
    assert split.decentral["Gas boiler"] == pytest.approx(16e-6 * 0.6)
    assert split.decentral["Ground-source heat pump"] == pytest.approx(16e-6 * 0.4)
    assert "Agriculture heat" not in split.decentral.index


def test_district_mix_ignores_storage_and_shares_with_industry(ctx):
    split = heat_split(ctx, "AA", 2025)
    produced = 2 * 4.2 + 2 * 0.5
    assert set(split.district_mix.index) == {"Gas CHP with capture", "Electric boiler"}
    assert split.district_mix["Gas CHP with capture"] == pytest.approx(6e-6 * 8.4 / produced)
    assert split.district_mix.sum() == pytest.approx(split.district)
    assert split.district_other == pytest.approx(2e-6)


def test_pie_points_the_district_slice_at_the_second_pie(ctx):
    split = heat_split(ctx, "AA", 2025)
    fig = heat_chart._pie_of_pie(split, {})
    main, sub = fig.data[0], fig.data[1]
    assert main.labels[0] == heat_chart.DISTRICT
    share = split.district / split.total
    assert main.rotation == pytest.approx(90 - 180 * share)
    assert main.pull[0] > 0 and list(sub.labels) == list(split.district_mix.index)
    # two connector lines, symmetric about the pies' common centre line
    lines = [t for t in fig.data if t.type == "scatter"]
    assert len(lines) == 2
    (x_top, y_top), (x_bot, y_bot) = [(t.x, t.y) for t in lines]
    assert x_top == pytest.approx(x_bot)
    assert y_top[0] - 0.5 == pytest.approx(0.5 - y_bot[0])
    assert all(0 <= v <= 1 for t in lines for v in (*t.x, *t.y))


def test_supply_mix_returns_html_with_the_numbers(ctx):
    html = heat_chart.supply_mix(ctx, "AA", SimpleNamespace(title="Heat", unit="TWh/year"))
    text = str(html)
    assert "District heating" in text and "Total residential and tertiary heat" in text


def test_no_heat_buses_no_section(ctx):
    ctx.locations_for = lambda code: ["ZZ"]
    assert heat_chart.supply_mix(ctx, "ZZ", SimpleNamespace(title="Heat", unit="TWh/year")) is None
