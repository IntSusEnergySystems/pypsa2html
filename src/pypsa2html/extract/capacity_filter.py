"""Select which ``nodal_capacities.csv`` rows belong on capacity charts.

PyPSA-Eur's nodal capacity export includes every controllable component:
fuel-potential Generators (biogas on a biogas bus), unconstrained industry
feedstock Links, heat vents and CO₂ / material Stores.  Their ``p_nom_opt`` /
``e_nom_opt`` is a modelling bound, not installed conversion capacity, and
dominates a stacked GW bar by many orders of magnitude.

Filtering uses the solved network topology — and the **component type** —
so the same carrier name can be kept as a Link (``lignite`` power plant) and
dropped as a Generator (``lignite`` fuel potential):

* **Power chart** — keep ``(component, carrier)`` pairs for Generators /
  Links / StorageUnits / Lines that attach to an electricity, heat or H2
  *service* bus (and pipelines).  Drop load-shedding, vents, and fuel
  Generators whose bus carrier equals the component carrier.
* **Storage chart** — keep Store carriers on battery / H2 / gas / water /
  NH3 buses; drop fuel and CO2 stocks.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

#: Bus carriers that deliver an energy service (not a primary commodity stock).
_SERVICE_BUSES_EXACT = frozenset({"AC", "low voltage", "H2"})

#: ``(component, carrier)`` as stored in ``nodal_capacities.csv``.
CapacityKey = tuple[str, str]


def _is_service_bus(bus_carrier: Any) -> bool:
    if bus_carrier is None or (isinstance(bus_carrier, float) and pd.isna(bus_carrier)):
        return False
    name = str(bus_carrier)
    if name in _SERVICE_BUSES_EXACT:
        return True
    # District / building heat buses; vent *components* are filtered by name.
    return "heat" in name and "vent" not in name


def _is_junk_name(carrier: Any) -> bool:
    name = str(carrier)
    lowered = name.lower()
    if lowered in {"load", "load shedding"}:
        return True
    return "vent" in lowered or "to air" in lowered or "shedding" in lowered


def _is_energy_store_bus(bus_carrier: Any) -> bool:
    if bus_carrier is None or (isinstance(bus_carrier, float) and pd.isna(bus_carrier)):
        return False
    name = str(bus_carrier)
    # H2 / methane stores and batteries / thermal stores.  NH3 and commodity
    # stocks (oil, coal, CO2, …) are excluded — their e_nom is typically an
    # unconstrained buffer, not a plotted storage capacity.
    if name in {"H2", "gas"} or "H2" in name:
        return True
    return "battery" in name or "water" in name


def _link_buses(row: pd.Series, bus_carrier: pd.Series) -> list[Any]:
    buses = [bus_carrier.get(row.bus0), bus_carrier.get(row.bus1)]
    for i in range(2, 6):
        col = f"bus{i}"
        if col not in row.index:
            continue
        val = row[col]
        if pd.isna(val) or val == "" or val is None:
            continue
        buses.append(bus_carrier.get(val))
    return buses


def capacity_keys_from_network(
    n: Any,
) -> tuple[frozenset[CapacityKey], frozenset[str]]:
    """Return ``(power_keys, storage_carriers)`` derived from ``n``.

    ``power_keys`` are ``(component, carrier)`` pairs matching the columns in
    ``nodal_capacities.csv`` (``Generator``, ``Link``, ``StorageUnit``,
    ``Line``, …).  ``storage_carriers`` are Store carrier names.
    """
    bus_carrier = n.buses["carrier"]
    power: set[CapacityKey] = set()
    storage: set[str] = set()

    gens = getattr(n, "generators", None)
    if gens is not None and not gens.empty:
        for row in gens.itertuples():
            carrier = str(row.carrier)
            if _is_junk_name(carrier):
                continue
            b = bus_carrier.get(row.bus)
            # Fuel potentials sit on a commodity bus named like the carrier.
            if _is_service_bus(b) and str(b) != carrier:
                power.add(("Generator", carrier))

    links = getattr(n, "links", None)
    if links is not None and not links.empty:
        for _, row in links.iterrows():
            carrier = str(row.carrier)
            if _is_junk_name(carrier):
                continue
            if "pipeline" in carrier:
                power.add(("Link", carrier))
                continue
            buses = _link_buses(row, bus_carrier)
            if any(_is_service_bus(b) for b in buses):
                power.add(("Link", carrier))

    storage_units = getattr(n, "storage_units", None)
    if storage_units is not None and not storage_units.empty:
        for row in storage_units.itertuples():
            b = bus_carrier.get(row.bus)
            if _is_service_bus(b):
                power.add(("StorageUnit", str(row.carrier)))

    lines = getattr(n, "lines", None)
    if lines is not None and not lines.empty and "carrier" in lines.columns:
        for c in lines["carrier"].dropna().unique():
            power.add(("Line", str(c)))

    stores = getattr(n, "stores", None)
    if stores is not None and not stores.empty:
        for row in stores.itertuples():
            b = bus_carrier.get(row.bus)
            if _is_energy_store_bus(b):
                storage.add(str(row.carrier))

    return frozenset(power), frozenset(storage)


# Backwards-compatible alias used in early drafts / docs.
def capacity_carriers_from_network(
    n: Any,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return carrier *names* only (loses Generator vs Link distinction)."""
    keys, storage = capacity_keys_from_network(n)
    return frozenset(carrier for _comp, carrier in keys), storage
