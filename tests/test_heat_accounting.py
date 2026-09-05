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

from pypsa2html.charts.results import _POWER_TO_FUEL, _panel_title
from pypsa2html.datafiles import load_taxonomy
from pypsa2html.extract.capacity_filter import (
    electric_output_scaling,
    heat_output_scaling,
)
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

# --------------------------------------------------------------------------
# power-plant capacity on the electrical side
# --------------------------------------------------------------------------
def _plant_network():
    """A fuel-side-rated fleet: PyPSA rates a Link's ``p_nom`` at ``bus0``.

    ``bus0`` is the *fuel* bus for a CCGT and a CHP, so the raw ``p_nom`` is
    MW_th.  The heat pump is here to guard the trap: it is a **reversed** link
    (``bus0`` heat, ``bus1`` electricity), so it matches "delivers electricity
    at bus1" by accident while its ``p_nom`` is already MW_th.
    """
    buses = pd.DataFrame(
        {
            "carrier": {
                "b_ac": "AC",
                "b_lv": "low voltage",
                "b_gas": "gas",
                "b_uranium": "uranium",
                "b_h2": "H2",
                "b_heat": "urban central heat",
                "b_batt": "battery",
            }
        }
    )
    links = pd.DataFrame(
        {
            "bus0": ["b_gas", "b_gas", "b_uranium", "b_ac", "b_ac",
                     "b_heat", "b_lv", "b_batt", "b_h2", "b_ac"],
            "bus1": ["b_ac", "b_ac", "b_ac", "b_h2", "b_lv",
                     "b_lv", "b_heat", "b_ac", "b_gas", "b_batt"],
            "bus2": ["", "b_heat", "", "", "", "", "", "", "", ""],
            "carrier": [
                "CCGT",
                "urban central gas CHP",
                "nuclear",
                "H2 Electrolysis",
                "electricity distribution grid",
                "urban central air heat pump",
                "urban central resistive heater",
                "battery discharger",
                "Sabatier",
                "battery charger",
            ],
            "efficiency": [0.57, 0.42, 0.33, 0.66, 0.97, 0.33, 0.9, 0.98, 0.8, 0.98],
            "p_nom_opt": [1000.0, 500.0, 2000.0, 100.0, 900.0,
                          300.0, 200.0, 400.0, 50.0, 400.0],
        },
        index=["ccgt", "chp", "nuc", "ely", "dist", "hp", "rh", "bd", "sab", "bc"],
    )
    return SimpleNamespace(buses=buses, links=links)


def _electric_factors(n=None):
    n = n if n is not None else _plant_network()
    return electric_output_scaling(n, exclude=heat_output_scaling(n))


def test_ccgt_is_restated_on_the_electrical_side():
    """The bug: a 1000 MW_th CCGT bar is 1.75x its 570 MW_e plate rating."""
    assert _electric_factors()["CCGT"] == pytest.approx(0.57)


def test_chp_is_restated_using_its_electric_port():
    """``efficiency`` is the bus0 -> bus1 (electric) ratio; heat is bus2."""
    assert _electric_factors()["urban central gas CHP"] == pytest.approx(0.42)


def test_nuclear_is_restated_on_the_electrical_side():
    assert _electric_factors()["nuclear"] == pytest.approx(0.33)


def test_battery_discharger_is_restated_so_it_matches_its_charger():
    """The charger is rated at ``bus0`` = AC and needs no correction, so
    without this the two halves of one battery show different capacities."""
    factors = _electric_factors()
    assert factors["battery discharger"] == pytest.approx(0.98)
    assert "battery charger" not in factors


def test_reversed_heat_pump_is_not_treated_as_a_generator():
    """The trap: ``bus1`` is the electricity bus, but that is its *input*.

    Scaling it by 1/COP would shrink an already-thermal p_nom; heat pumps stay
    MW_th under :func:`heat_output_scaling`.
    """
    factors = _electric_factors()
    assert "urban central air heat pump" not in factors
    assert heat_output_scaling(_plant_network())["urban central air heat pump"] == 1.0


def test_electricity_consuming_links_are_untouched():
    """Rated at ``bus0`` = electricity already: an electrolyser's p_nom is the
    MW_e it draws, and the distribution grid is electric on both sides."""
    factors = _electric_factors()
    for carrier in (
        "H2 Electrolysis",
        "electricity distribution grid",
        "urban central resistive heater",
        "battery charger",
    ):
        assert carrier not in factors


def test_non_electric_outputs_are_untouched():
    """Methanation delivers gas at ``bus1``; its p_nom stays MW of H2 input."""
    assert "Sabatier" not in _electric_factors()


def test_fleet_efficiency_is_capacity_weighted():
    """A carrier holds several vintages, and the factor multiplies the summed
    p_nom — so a 10 MW inefficient unit must not outvote a 1000 MW modern one.
    """
    n = _plant_network()
    n.links = pd.concat(
        [
            n.links,
            pd.DataFrame(
                {"bus0": ["b_gas"], "bus1": ["b_ac"], "bus2": [""],
                 "carrier": ["CCGT"], "efficiency": [0.30], "p_nom_opt": [10.0]},
                index=["ccgt_old"],
            ),
        ]
    )
    expected = (1000 * 0.57 + 10 * 0.30) / 1010
    assert _electric_factors(n)["CCGT"] == pytest.approx(expected)
    assert _electric_factors(n)["CCGT"] > 0.56  # an unweighted mean gives 0.435


def test_zero_capacity_still_yields_a_factor():
    """An empty horizon must not lose the carrier's rating convention."""
    n = _plant_network()
    n.links = n.links.assign(p_nom_opt=0.0)
    assert _electric_factors(n)["CCGT"] == pytest.approx(0.57)


def test_electric_and_heat_maps_are_disjoint():
    n = _plant_network()
    p2h = heat_output_scaling(n)
    assert not set(_electric_factors(n)) & set(p2h)


def test_empty_network_returns_no_electric_factors():
    empty = SimpleNamespace(buses=pd.DataFrame({"carrier": {}}), links=pd.DataFrame())
    assert electric_output_scaling(empty) == {}


def test_panel_titles_flag_every_non_electric_axis():
    """Panels that are not GW_e must say so; the plain electric ones must not."""
    assert _panel_title(["power-to-heat"]) == "Power-to-heat (thermal)"
    assert _panel_title(_POWER_TO_FUEL) == "Power-to-fuel (input-rated)"
    for group in (["CCGT"], ["CHP"], ["nuclear"], ["solar"]):
        assert "(" not in _panel_title(group)
