"""Network-cost sections: layer classification, the branch split, the views.

The sections answer a stakeholder review (Sept–Oct 2026) that read the model's
grid annuities as if they were the regulated network costs.  These tests pin
the three things the answer rests on:

* every layer of the calibrated table lands in the right existing / new /
  operation / add-on / losses bucket, and the memo row is never added;
* the per-neighbour branch view splits a branch 50/50 between its ends,
  counts a branch inside a group node in full, and separates the capacity
  the first horizon had from what the model added;
* the tariff view reproduces the cost-by-segment *Transport* composition and
  is shown only on the node the calibrated table describes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

from pypsa2html.charts import network_costs as nc
from pypsa2html.charts.base import Html
from pypsa2html.extract.network_costs import (
    INTERNAL,
    branch_costs,
    branch_wide,
    calibrated_table,
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _network(s_nom: float, s_nom_opt: float, dc_new: float = 0.0, year: int = 2025):
    """Three locations, AA-BB and AA-CC lines, a BB-CC line, a DC link AA-CC."""
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=2, freq="h"))
    for loc in ("AA", "BB", "CC"):
        n.add("Bus", loc, carrier="AC", location=loc)
    n.add("Line", "AA-BB", bus0="AA", bus1="BB", s_nom=s_nom, s_nom_opt=s_nom_opt,
          capital_cost=1000.0, x=0.1)
    n.add("Line", "AA-CC", bus0="AA", bus1="CC", s_nom=50.0, s_nom_opt=50.0,
          capital_cost=2000.0, x=0.1)
    n.add("Line", "BB-CC", bus0="BB", bus1="CC", s_nom=10.0, s_nom_opt=10.0,
          capital_cost=1000.0, x=0.1)
    n.add("Link", "DC AA-CC", bus0="AA", bus1="CC", carrier="DC", p_nom=20.0,
          p_nom_opt=20.0, capital_cost=500.0, build_year=2025)
    n.add("Link", "DC AA-CC-reversed", bus0="CC", bus1="AA", carrier="DC", p_nom=20.0,
          p_nom_opt=20.0, capital_cost=500.0, build_year=2025)
    if dc_new:
        n.add("Link", f"DC AA-CC-{year}", bus0="AA", bus1="CC", carrier="DC",
              p_nom=0.0, p_nom_opt=dc_new, capital_cost=500.0, build_year=year)
    return n


@pytest.fixture
def ctx(tmp_path):
    networks = {
        2025: _network(100.0, 100.0),
        2030: _network(100.0, 160.0, dc_new=40.0, year=2030),
    }
    groups = {"AA": ["AA"], "AB": ["AA", "BB"]}
    long = pd.DataFrame([
        ("electricity_distribution", "regulated", "L1", "existing grid capital charges", 2025, 390.0),
        ("electricity_distribution", "regulated", "L3", "OPEX (asset + per connection)", 2025, 290.0),
        ("electricity_distribution", "model", "L2", "reinforcement (link vintages after the first horizon)", 2025, 0.0),
        ("electricity_distribution", "model", "L2", "reinforcement (link vintages after the first horizon)", 2030, 50.0),
        ("electricity_distribution", "regulated", "L1", "existing grid capital charges", 2030, 390.0),
        ("electricity_distribution", "regulated", "L3", "OPEX (asset + per connection)", 2030, 295.0),
        ("electricity_distribution", "model", "memo", "model valuation of the first-horizon vintage (not added)", 2025, 128.0),
        ("electricity_distribution", "model", "memo", "model valuation of the first-horizon vintage (not added)", 2030, 128.0),
        ("electricity_distribution", "model", "L4", "losses (energy at the HV price; in Production/Imports)", 2025, 80.0),
        ("electricity_distribution", "regulated", "L4", "road-use fee", 2025, 44.0),
        ("gas_distribution", "regulated", "L1", "existing grid capital charges (keep pathway)", 2025, 193.0),
        ("electricity_transmission", "tariff", "L1+L3", "Elia 2025 allowed revenue x Walloon offtake share", 2025, 378.0),
        ("electricity_transmission", "tariff", "L1+L3", "Elia 2025 allowed revenue x Walloon offtake share", 2030, 415.0),
        ("electricity_transmission", "tariff", "L2", "Belgian branch increments x Walloon offtake share", 2030, 20.0),
        ("electricity_transmission", "direct", "L1", "existing branches, 50/50 (model cost)", 2025, 43.0),
        ("methane_transmission", "tariff", "L1+L3", "Fluxys revenue x Walloon gas offtake share", 2025, 71.0),
        ("CO2_transmission", "direct", "L2", "new branch capacity, 50/50 (model cost)", 2030, 26.0),
        ("electricity_transmission", "share", "-", "Walloon offtake share", 2025, 0.244),
    ], columns=["network", "view", "layer", "item", "year", "value"])
    (tmp_path / "csvs").mkdir()
    long.to_csv(tmp_path / "csvs" / "costs.csv", index=False)

    def read_csv(relpath, base="results", **kwargs):
        path = tmp_path / relpath
        return pd.read_csv(path, **kwargs) if path.exists() else None

    return SimpleNamespace(
        networks=networks,
        horizons=[2025, 2030],
        year_columns=["2025", "2030"],
        locations_for=lambda code: groups.get(code, [code]),
        nodes=_Nodes({"AA": "Alpha", "AB": "Alpha+Beta", "BB": "Beta", "CC": "Gamma"}),
        read_csv=read_csv,
        _files={},
        config=SimpleNamespace(
            features=SimpleNamespace(network_costs={
                "node": "AA", "file": "csvs/costs.csv", "segments_file": None,
                "references": {"electricity_distribution": [
                    {"year": 2025, "value": 896.3, "label": "Regulator 2025"}]},
            }),
            output=SimpleNamespace(plotly="cdn", chart_data=False),
            nodes=SimpleNamespace(labels={}),
        ),
    )


class _Nodes:
    def __init__(self, labels):
        self._labels = labels

    def __getitem__(self, code):
        return SimpleNamespace(label=self._labels[code])


SECTION = SimpleNamespace(id="x", title="Network costs", unit="MEUR/year")


# --------------------------------------------------------------------------
# layer classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(("network", "layer", "item", "status"), [
    ("electricity_distribution", "L1", "existing grid capital charges", "existing"),
    ("electricity_distribution", "L3", "OPEX (asset + per connection)", "operation"),
    ("electricity_distribution", "L2", "reinforcement (link vintages after the first horizon)", "new"),
    ("electricity_distribution", "L2", "rooftop PV hosting (inside the PV capital cost)", "new"),
    ("electricity_distribution", "L4", "losses (energy at the HV price; in Production/Imports)", "losses"),
    ("electricity_distribution", "L4", "smart meters", "metering"),
    ("electricity_distribution", "L4", "public service obligations", "addon"),
    ("electricity_distribution", "memo", "model valuation of the first-horizon vintage (not added)", "memo"),
    ("gas_distribution", "L1", "existing grid capital charges (keep pathway)", "existing"),
    ("some_network", "L4", "transmission losses", "losses"),
    ("some_network", "L2", "anything new", "new"),
])
def test_layers_map_to_status(network, layer, item, status):
    assert nc._describe(network, layer, item)[1] == status


def test_calibrated_table_is_only_for_its_node(ctx):
    assert calibrated_table(ctx, "AA") is not None
    assert calibrated_table(ctx, "BB") is None
    assert calibrated_table(ctx, "AB") is None


def test_memo_row_is_listed_but_not_added(ctx):
    pivot = nc.wide(calibrated_table(ctx, "AA"), ["2025", "2030"])
    rows, memos = nc._distribution_rows(pivot, "electricity_distribution")
    assert [m.status for m in memos] == ["memo"]
    total = sum(r.values for r in rows)
    # 390 + 290 + 0 + 80 + 44 in 2025: the 128 memo is not in it
    assert total["2025"] == pytest.approx(804.0)
    assert total["2030"] == pytest.approx(390.0 + 295.0 + 50.0)


# --------------------------------------------------------------------------
# branch split
# --------------------------------------------------------------------------
def test_branches_split_half_and_existing_vs_new(ctx):
    table = branch_wide(branch_costs(ctx, "AA"), ["2025", "2030"]) * 1e6
    elec = table.xs("electricity", level="network")
    # AA-BB: 100 MW existing at 1000 €/MW, half to AA = 50 k€; 2030 adds 60 MW.
    assert elec.loc[("BB", "existing"), "2025"] == pytest.approx(50_000)
    assert elec.loc[("BB", "existing"), "2030"] == pytest.approx(50_000)
    assert elec.loc[("BB", "new"), "2030"] == pytest.approx(0.5 * 60 * 1000)
    # AA-CC line (50 MW x 2000) + DC (20 MW x 500, reversed twin not counted),
    # halves; the 2030 DC vintage (40 MW) is new in full.
    assert elec.loc[("CC", "existing"), "2025"] == pytest.approx(0.5 * (50 * 2000 + 20 * 500))
    assert elec.loc[("CC", "new"), "2030"] == pytest.approx(0.5 * 40 * 500)
    assert ("BB", "new") not in elec.index or elec.loc[("BB", "new"), "2025"] == 0


def test_branch_inside_a_group_counts_in_full(ctx):
    table = branch_wide(branch_costs(ctx, "AB"), ["2025", "2030"]) * 1e6
    elec = table.xs("electricity", level="network")
    assert elec.loc[(INTERNAL, "existing"), "2025"] == pytest.approx(100 * 1000)
    # BB-CC is a link to a neighbour of the group: half
    assert elec.loc[("CC", "existing"), "2025"] == pytest.approx(
        0.5 * (50 * 2000 + 20 * 500) + 0.5 * 10 * 1000
    )


def test_no_branch_view_for_the_study_wide_node(ctx):
    ctx.locations_for = lambda code: None
    assert branch_costs(ctx, "ALL") is None


# --------------------------------------------------------------------------
# views and builders
# --------------------------------------------------------------------------
def test_tariff_view_has_the_segment_composition(ctx):
    pivot = nc.wide(calibrated_table(ctx, "AA"), ["2025", "2030"])
    rows, info = nc._tariff_rows(pivot, "Alpha")
    statuses = {r.label.split(":")[0]: r.status for r in rows}
    assert statuses["Methane"] == "existing"
    assert statuses["CO₂"] == "new"
    total = sum(r.values for r in rows)
    # tariff electricity (existing + new) + tariff methane + CO2 direct;
    # the direct electricity row (43) is the branch view, not added here.
    assert total["2025"] == pytest.approx(378 + 71)
    assert total["2030"] == pytest.approx(415 + 20 + 26)
    assert "24.4 %" in info["electricity"][0]
    assert {r.network for r in rows} == {"electricity", "methane", "CO2"}


def test_counted_layers_exclude_transfers_and_losses(ctx):
    """The red mark must equal what the cost-by-segment chart counts."""
    pivot = nc.wide(calibrated_table(ctx, "AA"), ["2025", "2030"])
    rows, _ = nc._distribution_rows(pivot, "electricity_distribution")
    counted = nc._counted(rows, ["2025", "2030"])
    # 2025: 390 existing + 290 OPEX; not the 80 losses nor the 44 road-use fee
    assert counted["2025"] == pytest.approx(680.0)
    assert counted["2030"] == pytest.approx(390.0 + 295.0 + 50.0)


def test_distribution_section_only_on_its_node(ctx):
    html_aa = nc.distribution(ctx, "AA", SECTION)
    assert isinstance(html_aa, Html)
    text = str(html_aa)
    assert "Electricity distribution" in text and "Gas distribution" in text
    assert "Regulator 2025" in text
    assert "Memo, not added" in text
    assert "of which counted in the total system cost" in text
    assert nc.distribution(ctx, "BB", SECTION) is None


def test_first_transmission_view_is_electricity_only(ctx, monkeypatch):
    """The all-networks tariff chart was replaced by an electricity-only one
    (2026-10-06): existing grid inside the region and its interconnectors, and
    new interconnections. Gas, H2 and CO2 stay reconcilable in a table."""
    captured = []
    monkeypatch.setattr(nc, "figure_to_html", lambda fig, **kw: captured.append(fig) or "")
    text = str(nc.transmission(ctx, "AA", SECTION))
    fig = captured[0]
    first = fig.layout.updatemenus[0].buttons[0]
    assert first.label.startswith("Electricity: what Alpha pays")
    shown = [t for t, v in zip(fig.data, first.args[0]["visible"], strict=True) if v]
    assert len(shown) == 2
    assert sum(t.y[1] for t in shown) == pytest.approx(415 + 20)   # 2030
    assert "Total electricity transmission" in text
    assert "Other transmission networks" in text and "Total other networks" in text
    assert "all networks" not in text


def test_transmission_section_states_what_is_not_modelled(ctx):
    text = str(nc.transmission(ctx, "AA", SECTION))
    assert "not modelled" in text
    assert "Interconnection with Beta" in text
    # other nodes still get the branch view
    other = nc.transmission(ctx, "BB", SECTION)
    assert other is not None and "Interconnection with Alpha" in str(other)
