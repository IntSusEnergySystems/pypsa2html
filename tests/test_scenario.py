"""The comparison charts read scenario summaries, never networks."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd

from pypsa2html.charts import scenario as sc
from pypsa2html.datafiles import load_taxonomy
from tests.test_metrics import FakeData


def _ctx(scenarios: dict, reference="A"):
    config = SimpleNamespace(
        project=SimpleNamespace(decimals=3),
        output=SimpleNamespace(chart_data=False),
        features=SimpleNamespace(nuclear_primary="uranium"),
        raw={},
    )
    return SimpleNamespace(config=config, taxonomy=load_taxonomy(), scenarios=scenarios,
                           reference=reference, _files={})


def _costs(solar: float, gas: float) -> pd.DataFrame:
    return pd.DataFrame({"2030": [solar, gas], "2050": [solar * 2, gas / 2]},
                        index=["solar", "gas"])


SECTION = SimpleNamespace(id="scenario_costs", title="Annual costs", unit="EUR/year")


def _buttons(fig):
    return [b.label for b in fig.layout.updatemenus[0].buttons]


def test_table_comparison_has_totals_compositions_and_differences():
    a = FakeData(tables={("AA", "cost.total"): _costs(10.0, 20.0)}, label="A")
    b = FakeData(tables={("AA", "cost.total"): _costs(15.0, 10.0)}, label="B")
    fig = sc.costs(_ctx({"A": a, "B": b}), "AA", SECTION)
    assert _buttons(fig) == ["Total", "A", "B", "B − A", "Difference to A"]


def test_the_difference_view_is_technology_by_technology():
    a = FakeData(tables={("AA", "cost.total"): _costs(10.0, 20.0)}, label="A")
    b = FakeData(tables={("AA", "cost.total"): _costs(15.0, 10.0)}, label="B")
    fig = sc.costs(_ctx({"A": a, "B": b}), "AA", SECTION)
    traces = {t.name: t for t in fig.data if t.name in ("solar", "gas")}
    # The difference view's solar trace: +5 in 2030, +10 in 2050.
    solar = [t for t in fig.data if t.name == "solar"]
    assert any(list(t.y) == [5.0, 10.0] for t in solar)
    assert traces


def test_a_single_scenario_has_no_difference_view():
    a = FakeData(tables={("AA", "cost.total"): _costs(10.0, 20.0)}, label="A")
    fig = sc.costs(_ctx({"A": a}), "AA", SECTION)
    assert getattr(fig.layout, "updatemenus", None) in (None, ()) or \
        "Difference to A" not in _buttons(fig)


def test_a_scenario_without_the_table_is_left_out(caplog):
    a = FakeData(tables={("AA", "cost.total"): _costs(10.0, 20.0)}, label="A")
    b = FakeData(label="B")
    with caplog.at_level(logging.WARNING):
        fig = sc.costs(_ctx({"A": a, "B": b}), "AA", SECTION)
    assert "no cost.total for scenario B" in caplog.text
    assert fig is not None


def test_no_scenario_no_chart():
    assert sc.costs(_ctx({}), "AA", SECTION) is None


def _ratio(primary, electricity) -> pd.DataFrame:
    return pd.DataFrame({"2030": [primary, electricity], "2050": [primary, electricity]},
                        index=["primary", "electricity"])


def test_energy_independence_names_mixed_nuclear_conventions():
    a = FakeData(tables={("AA", "self_sufficiency.ratio"): _ratio(20, 100)}, label="A",
                 meta={"settings": {"features.nuclear_primary": "uranium"}})
    b = FakeData(tables={("AA", "self_sufficiency.ratio"): _ratio(30, 90)}, label="B",
                 meta={"settings": {"features.nuclear_primary": "electricity"}})
    section = SimpleNamespace(id="energy_independence", title="Energy independence", unit="%")
    fig = sc.energy_independence(_ctx({"A": a, "B": b}), "AA", section)
    assert any("nuclear conventions differ" in label for label in _buttons(fig))


def test_cumulative_emissions_reads_the_indicator_table():
    table = pd.DataFrame({"2030": [10.0], "2050": [30.0]}, index=["ind_ghg"])
    a = FakeData(tables={("AA", "indicator.ghg_sector_cum"): table}, label="A")
    section = SimpleNamespace(id="cumulative_emissions", title="Cumulative emissions",
                              unit="MtCO2eq")
    fig = sc.cumulative_emissions(_ctx({"A": a}), "AA", section)
    assert fig is not None
    assert list(fig.data[0].x) == [2030, 2050]
