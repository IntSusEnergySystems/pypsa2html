"""A heat pump is ambient heat plus electricity, and the two must add up.

The rows this module checks are what the taxonomy hangs ``pac_fe -> res`` and
``elc_fe -> res`` on.  Getting the second one wrong does not show up as a wrong
heat number — the heat charts read the network directly — it shows up as an
electricity node that does not conserve energy, which is why the regression that
this guards went unnoticed for as long as it did.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.extract.flows import _heat_pump_rows, _rank_entries


def _pump_network():
    """One reversed heat pump: ``bus0`` heat, ``bus1`` low voltage, COP 3."""
    buses = pd.DataFrame({"carrier": {"b_heat": "rural heat", "b_lv": "low voltage"}})
    links = pd.DataFrame(
        {
            "bus0": ["b_heat"],
            "bus1": ["b_lv"],
            "carrier": ["rural ground heat pump"],
            "efficiency": [1 / 3.0],
        },
        index=["hp"],
    )
    # Only the port keys are read off links_t here; the values come from `totals`.
    links_t = {"p0": None, "p1": None}
    return SimpleNamespace(buses=buses, links=links, links_t=links_t)


#: 9 TWh of heat out at COP 3 -> 3 TWh of electricity in, 6 TWh of ambient.
HEAT_OUT, ELECTRICITY, AMBIENT = 9.0, 3.0, 6.0


def _totals():
    idx = pd.Index(["hp"])
    return {
        0: pd.Series([-HEAT_OUT], index=idx),
        1: pd.Series([ELECTRICITY], index=idx),
    }


def _port_rows():
    """What ``_link_flows`` produces for that pump, before the split."""
    return pd.DataFrame(
        {
            "carrier": ["rural ground heat pump", "rural ground heat pump"],
            "source": ["rural heat", "rural heat"],
            "target": ["low voltage", "losses"],
            "value": [ELECTRICITY, -AMBIENT],
            "origin": ["link", "link"],
            "port": [1, 2],
        }
    )


def test_the_two_rows_are_ambient_and_electricity_and_they_sum_to_the_heat():
    out = _heat_pump_rows(_pump_network(), pd.Index(["hp"]), _port_rows(), totals=_totals())
    values = sorted(out["value"].abs())
    assert values == pytest.approx([ELECTRICITY, AMBIENT])
    assert sum(values) == pytest.approx(HEAT_OUT)


def test_the_second_entry_is_the_electricity_draw():
    """``rural ground heat pump_2`` is what the taxonomy books as electricity.

    It used to carry the heat *output*, so the Sankey charged the electricity
    node for the ambient intake as well — 9.3 TWh on BEWAL 2050 alone.
    """
    out = _heat_pump_rows(_pump_network(), pd.Index(["hp"]), _port_rows(), totals=_totals())
    out["sign"] = (out["value"] > 0).astype(int)
    out["value"] = out["value"].abs()
    out["entry"] = _rank_entries(out)
    by_entry = out.set_index("entry")["value"]
    assert by_entry["rural ground heat pump"] == pytest.approx(AMBIENT)
    assert by_entry["rural ground heat pump_2"] == pytest.approx(ELECTRICITY)


def test_the_losses_row_is_dropped():
    """The "losses" of a heat pump are the ambient intake with the other sign."""
    out = _heat_pump_rows(_pump_network(), pd.Index(["hp"]), _port_rows(), totals=_totals())
    assert "losses" not in set(out["target"])
