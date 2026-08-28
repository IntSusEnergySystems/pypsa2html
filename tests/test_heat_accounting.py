"""Heat is reported in full and on one side of the meter.

Two accounting bugs found while reviewing pypsa-wal's 2026-08-27 meeting items
5 and 7 (``docs/temporary_improvement_plans.md`` in that repo):

* **Sectoral demands dropped a whole heat bus.**  ``carrier_flows_energy.csv``
  emits ``demandheatc`` for ``urban decentral heat``, but ``_DEMAND_CODES``
  listed ``demandheat``/``a``/``b``/``s`` and not ``c``.  The chart is a
  whitelist, so the largest residential/tertiary block vanished silently — 42.0
  of 59.2 TWh of Flemish heat in 2040 — and because the dropped block shrinks
  while district heating grows, Flanders appeared to *gain* heat demand
  (17.2 -> 21.6 TWh) while its true total *fell* (59.2 -> 52.1 TWh).

* **Power-to-heat mixed MW_th with MW_e.**  Heat pumps are reversed links
  (``bus0`` = heat), so their ``p_nom`` is thermal; resistive heaters are
  normal links, so theirs is electric.  The panel summed both.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.charts.results import _panel_title
from pypsa2html.datafiles import load_taxonomy
from pypsa2html.extract.capacity_filter import heat_output_scaling
from pypsa2html.extract.tables import _DEMAND_CODES


# --------------------------------------------------------------------------
# item 7 — every thermal demand reaches the chart
# --------------------------------------------------------------------------
def test_urban_decentral_heat_demand_is_mapped():
    assert "demandheatc" in _DEMAND_CODES
    assert _DEMAND_CODES["demandheatc"][0] == "heat"


def test_all_three_residential_heat_buses_land_on_a_sector():
    """rural + urban decentral + urban central must all be plotted."""
    flows = load_taxonomy().carrier_flows_energy
    codes = {}
    for bus in ("rural heat", "urban decentral heat", "urban central heat"):
        hit = flows.loc[flows["entry"].astype(str) == bus]
        assert not hit.empty, f"{bus} missing from carrier_flows_energy.csv"
        codes[bus] = str(hit["code"].iloc[0])

    for bus, code in codes.items():
        assert code in _DEMAND_CODES, f"{bus} -> {code} is not plotted"

    # decentral and rural share one sector; central is district heating
    assert (
        _DEMAND_CODES[codes["rural heat"]][1]
        == _DEMAND_CODES[codes["urban decentral heat"]][1]
    )
    assert (
        _DEMAND_CODES[codes["urban central heat"]][1]
        != _DEMAND_CODES[codes["rural heat"]][1]
    )


def test_no_primary_demand_carrier_is_unmapped():
    """The tripwire that would have caught the missing bus.

    ``_N``-suffixed rows are secondary link ports carrying energy already
    counted on the primary row, so they are excluded.
    """
    flows = load_taxonomy().carrier_flows_energy
    labelled = flows.loc[
        flows["label"].astype(str).str.contains("demand", case=False, na=False)
    ]
    primary = labelled.loc[
        ~labelled["entry"].astype(str).str.contains(r"_\d+$", regex=True, na=False)
    ]
    missing = sorted(
        {str(c) for c in primary["code"] if str(c) and str(c) not in _DEMAND_CODES}
    )
    assert not missing, f"demand codes emitted but never plotted: {missing}"


# --------------------------------------------------------------------------
# item 5 — power-to-heat capacity on a single side
# --------------------------------------------------------------------------
def _p2h_network(hp_efficiency: float = 0.33, rh_efficiency: float = 0.9):
    """Heat pump reversed (heat -> electricity); resistive heater normal."""
    buses = pd.DataFrame(
        {"carrier": {"b_lv": "low voltage", "b_heat": "urban central heat"}}
    )
    links = pd.DataFrame(
        {
            "bus0": ["b_heat", "b_lv", "b_lv"],
            "bus1": ["b_lv", "b_heat", "b_heat"],
            "carrier": [
                "urban central air heat pump",
                "urban central resistive heater",
                "urban central gas boiler",
            ],
            "efficiency": [hp_efficiency, rh_efficiency, 0.95],
        },
        index=["hp", "rh", "boiler"],
    )
    return SimpleNamespace(buses=buses, links=links)


def test_reversed_heat_pump_is_left_alone():
    """Its p_nom is already MW_th — scaling it would be the second bug."""
    assert heat_output_scaling(_p2h_network())["urban central air heat pump"] == 1.0


def test_resistive_heater_is_scaled_to_thermal_output():
    factors = heat_output_scaling(_p2h_network(rh_efficiency=0.9))
    assert factors["urban central resistive heater"] == pytest.approx(0.9)


def test_non_power_to_heat_links_are_untouched():
    """Only the carriers the power-to-heat panel sums are restated."""
    assert "urban central gas boiler" not in heat_output_scaling(_p2h_network())


def test_implausible_efficiency_is_refused_not_applied():
    n = _p2h_network(rh_efficiency=0.0)
    assert "urban central resistive heater" not in heat_output_scaling(n)


def test_scaling_makes_the_two_technologies_addable():
    """The regression: 1 MW_th of HP + 1 MW_e of resistive is not 2 MW."""
    factors = heat_output_scaling(_p2h_network())
    hp_th = 1000.0 * factors["urban central air heat pump"]
    rh_th = 1000.0 * factors["urban central resistive heater"]
    assert hp_th == 1000.0
    assert rh_th == pytest.approx(900.0)
    assert hp_th + rh_th == pytest.approx(1900.0)  # not the raw 2000


def test_empty_network_returns_no_factors():
    empty = SimpleNamespace(
        buses=pd.DataFrame({"carrier": {}}), links=pd.DataFrame()
    )
    assert heat_output_scaling(empty) == {}


def test_panel_title_flags_the_thermal_axis():
    assert _panel_title(["power-to-heat"]) == "Power-to-heat (thermal)"
    assert "(" not in _panel_title(["solar"])
