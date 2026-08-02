"""Network-topology capacity carrier classification."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from pypsa2html.extract.capacity_filter import capacity_keys_from_network


def _toy_network() -> SimpleNamespace:
    """Minimal duck-typed network covering the classification rules."""
    buses = pd.DataFrame(
        {
            "carrier": {
                "b_ac": "AC",
                "b_lv": "low voltage",
                "b_heat": "urban central heat",
                "b_h2": "H2",
                "b_gas": "gas",
                "b_biogas": "biogas",
                "b_bm": "solid biomass",
                "b_oil": "oil",
                "b_naphtha": "naphtha for industry",
                "b_lignite": "lignite",
                "b_batt": "battery",
                "b_co2": "co2",
            }
        }
    )
    generators = pd.DataFrame(
        {
            "bus": ["b_ac", "b_biogas", "b_bm", "b_heat", "b_gas", "b_lignite"],
            "carrier": [
                "solar",
                "biogas",
                "solid biomass",
                "urban central heat vent",
                "gas",
                "lignite",  # fuel potential — must NOT keep as Generator
            ],
        },
        index=["g1", "g2", "g3", "g4", "g5", "g6"],
    )
    links = pd.DataFrame(
        {
            "bus0": ["b_gas", "b_gas", "b_bm", "b_oil", "b_gas", "b_h2", "b_lignite"],
            "bus1": ["b_ac", "b_heat", "b_heat", "b_naphtha", "b_gas", "b_h2", "b_ac"],
            "bus2": [None, None, None, None, None, None, None],
            "carrier": [
                "CCGT",
                "urban central gas boiler",
                "urban central solid biomass CHP",
                "naphtha for industry",
                "gas pipeline",
                "H2 pipeline",
                "lignite",  # power plant Link — keep
            ],
        },
        index=["l1", "l2", "l3", "l4", "l5", "l6", "l7"],
    )
    storage_units = pd.DataFrame(
        {"bus": ["b_ac"], "carrier": ["PHS"]},
        index=["su1"],
    )
    stores = pd.DataFrame(
        {
            "bus": ["b_batt", "b_gas", "b_h2", "b_co2", "b_oil"],
            "carrier": ["battery", "gas", "H2 Store", "co2", "oil"],
        },
        index=["s1", "s2", "s3", "s4", "s5"],
    )
    lines = pd.DataFrame({"carrier": ["AC"]}, index=["line1"])
    return SimpleNamespace(
        buses=buses,
        generators=generators,
        links=links,
        storage_units=storage_units,
        stores=stores,
        lines=lines,
    )


def test_power_keeps_service_attached_drops_fuel_and_vents():
    power, storage = capacity_keys_from_network(_toy_network())
    assert ("Generator", "solar") in power
    assert ("Link", "CCGT") in power
    assert ("Link", "urban central gas boiler") in power
    assert ("Link", "urban central solid biomass CHP") in power
    assert ("StorageUnit", "PHS") in power
    assert ("Line", "AC") in power
    assert ("Link", "gas pipeline") in power
    assert ("Link", "H2 pipeline") in power
    # Same carrier name: Link power plant kept, Generator fuel potential dropped
    assert ("Link", "lignite") in power
    assert ("Generator", "lignite") not in power

    assert ("Generator", "biogas") not in power
    assert ("Generator", "solid biomass") not in power
    assert ("Generator", "gas") not in power
    assert ("Generator", "urban central heat vent") not in power
    assert ("Link", "naphtha for industry") not in power


def test_storage_keeps_energy_stores_drops_commodity_stocks():
    _power, storage = capacity_keys_from_network(_toy_network())
    assert storage == frozenset({"battery", "gas", "H2 Store"})
    assert "co2" not in storage
    assert "oil" not in storage
