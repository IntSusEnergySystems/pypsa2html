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


#: Carrier substrings whose ``p_nom`` must be restated on the heat side before
#: they can be summed into one "power-to-heat" bar.  Matches the ``capacities``
#: view rules in ``tech_groups.csv``.
_P2H_PATTERNS = ("heat pump", "resistive heater")


def _is_heat_bus(bus_carrier: Any) -> bool:
    if bus_carrier is None or (isinstance(bus_carrier, float) and pd.isna(bus_carrier)):
        return False
    name = str(bus_carrier)
    return "heat" in name and "vent" not in name


def heat_output_scaling(n: Any) -> dict[str, float]:
    """Factors that put every power-to-heat ``p_nom`` on the **heat** side.

    Power-to-heat capacities are not comparable as PyPSA stores them:

    * a **heat pump** is a *reversed* link — ``bus0`` is the heat bus and
      ``bus1`` the electricity bus, with ``efficiency`` = 1/COP — so its
      ``p_nom`` is already **MW_th**;
    * a **resistive heater** is a normal link — ``bus0`` electricity,
      ``bus1`` heat — so its ``p_nom`` is **MW_e**.

    Summing them into one bar adds MW_th to MW_e and overstates heat pumps by
    the COP relative to resistive heaters.  Returning to a single side is the
    only way the "power-to-heat" panel means anything.

    The heat side is chosen because it needs no assumption: heat-pump ``p_nom``
    is already thermal, while the electrical side of a heat pump has no fixed
    value at all (``efficiency`` is a time series).  Resistive heaters are
    scaled by their own efficiency, which is a scalar.

    Returns ``{carrier: factor}`` for power-to-heat Link carriers only;
    carriers already on the heat side get ``1.0``.  Carriers absent from the
    mapping must be left untouched by the caller.
    """
    out: dict[str, float] = {}
    links = getattr(n, "links", None)
    if links is None or links.empty:
        return out
    bus_carrier = n.buses["carrier"]

    for carrier, group in links.groupby(links["carrier"].astype(str)):
        lowered = carrier.lower()
        if not any(p in lowered for p in _P2H_PATTERNS):
            continue
        row = group.iloc[0]
        if _is_heat_bus(bus_carrier.get(row.bus0)):
            out[carrier] = 1.0  # reversed link: p_nom is already MW_th
            continue
        if not _is_heat_bus(bus_carrier.get(row.bus1)):
            continue  # not an electricity -> heat link; leave alone
        eff = pd.to_numeric(group["efficiency"], errors="coerce").dropna()
        factor = float(eff.mean()) if len(eff) else 1.0
        if not (0.0 < factor <= 1.0):
            logger.warning(
                "power-to-heat: implausible efficiency %.3f for %r; not scaling",
                factor,
                carrier,
            )
            continue
        out[carrier] = factor

    return out


#: Bus carriers that *are* electricity.  ``low voltage`` is the distribution
#: side of the same commodity, so a link delivering there is still a generator.
_ELECTRIC_BUSES = frozenset({"AC", "low voltage"})


def _is_electric_bus(bus_carrier: Any) -> bool:
    if bus_carrier is None or (isinstance(bus_carrier, float) and pd.isna(bus_carrier)):
        return False
    return str(bus_carrier) in _ELECTRIC_BUSES


def electric_output_scaling(
    n: Any, *, exclude: Any = ()
) -> dict[str, float]:
    """Factors that put an electricity-producing Link's ``p_nom`` in **MW_e**.

    PyPSA rates a Link's ``p_nom`` at ``bus0``, and for a thermal power plant
    ``bus0`` is the *fuel* bus: a CCGT's ``p_nom`` is MW of gas, a CHP's is MW
    of gas or biomass, a nuclear plant's is MW of uranium heat.  PyPSA-Eur
    stores it that way on purpose (it multiplies the per-MW_e ``capital_cost``
    by the efficiency to match), and the vintages prove it — the brownfield
    values divide cleanly by the efficiency into round plate ratings
    (``447.368 = 255 / 0.57``).

    Plotted raw, a CCGT bar is 1.75x its electrical rating and cannot be
    compared with the nuclear bar beside it (which
    :func:`~pypsa2html.extract.tables._nuclear_electric_mw` already restates)
    nor with a wind bar (a Generator, always MW_e).  This returns
    ``{carrier: efficiency}`` so the caller can put them all on the electrical
    side, which is how a power plant is universally rated.

    A carrier qualifies when ``bus0`` is **not** electricity and ``bus1`` —
    the port ``efficiency`` describes — **is**.  Using ``bus1`` only is what
    keeps the rule safe: a CHP's heat port is ``bus2`` with its own
    ``efficiency2``, and DAC or Haber-Bosch draw electricity at ``bus0`` or a
    later port, so neither is touched.

    ``exclude`` carriers are skipped whatever their topology.  Power-to-heat is
    passed in that way, because a **heat pump is a reversed link** — ``bus0``
    is the heat bus and ``bus1`` the electricity bus — so it matches the rule
    by accident while its ``p_nom`` is already MW_th and belongs to
    :func:`heat_output_scaling` instead.
    """
    out: dict[str, float] = {}
    links = getattr(n, "links", None)
    if links is None or links.empty:
        return out
    bus_carrier = n.buses["carrier"]

    excluded = set(exclude)
    for carrier, group in links.groupby(links["carrier"].astype(str)):
        if carrier in excluded:
            continue  # power-to-heat: heat_output_scaling owns these
        row = group.iloc[0]
        if _is_electric_bus(bus_carrier.get(row.bus0)):
            continue  # already rated on the electricity side
        if not _is_electric_bus(bus_carrier.get(row.bus1)):
            continue  # does not deliver electricity at its primary port
        factor = _fleet_efficiency(group)
        if factor is None:
            continue
        if not (0.0 < factor <= 1.0):
            logger.warning(
                "electric rating: implausible efficiency %.3f for %r; not scaling",
                factor,
                carrier,
            )
            continue
        out[carrier] = factor

    return out


def _fleet_efficiency(group: pd.DataFrame) -> float | None:
    """Capacity-weighted mean electric efficiency of one carrier's Links.

    A carrier holds several vintages with different efficiencies (a 1995 CCGT
    beside a new-build one), and the factor multiplies the *summed* ``p_nom``
    of the whole group.  Weighting by capacity makes that product exact for
    the aggregate; an unweighted mean would let a tiny inefficient vintage
    drag the whole bar down.  Falls back to the plain mean when no capacity is
    installed, so the factor is still defined for an empty horizon.
    """
    eff = pd.to_numeric(group["efficiency"], errors="coerce")
    cap_col = "p_nom_opt" if "p_nom_opt" in group.columns else "p_nom"
    cap = pd.to_numeric(group.get(cap_col), errors="coerce")
    if cap is not None:
        valid = eff.notna() & cap.notna() & (cap > 0)
        if valid.any():
            return float((eff[valid] * cap[valid]).sum() / cap[valid].sum())
    eff = eff.dropna()
    return float(eff.mean()) if len(eff) else None
