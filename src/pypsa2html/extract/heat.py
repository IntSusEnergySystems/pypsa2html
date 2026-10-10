"""How the residential and tertiary heat demand of a node is met.

The question is "which technology delivers the heat that homes and offices
use", including the part that arrives through a district-heating network, and
then "what feeds that network".  A plain energy balance of the heat buses does
not answer it, for three reasons this module deals with explicitly:

* **Other users share the buses.**  PyPSA-Eur puts agriculture heat on a
  decentral heat bus and low-temperature industrial heat (and DAC) on the
  district-heating bus.  Each bus's supply is therefore apportioned to the
  residential/tertiary load *pro rata* to that load's share of everything the
  bus delivers.
* **Storage only shifts heat in time.**  Water-tank and pit chargers and
  dischargers are left out of the mix; their small round-trip loss is carried
  by the producers, pro rata, through the same apportioning.
* **Venting and other withdrawals** count as demand on the bus, not as
  negative supply.

So, per heat bus ``b`` and technology ``t``::

    delivered[t] = supply[t] / sum(supply) * residential_tertiary_load[b]

which sums exactly to the residential/tertiary load.  The district-heating
bus contributes one slice ("district heating") to the first pie and its own
technology split to the second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from ..context import BuildContext

logger = logging.getLogger(__name__)

#: Load carriers on heat buses that are *not* residential/tertiary demand.
NON_BUILDING_LOADS = ("industry", "agricultur", "DAC")

#: Link / store carriers that move heat in time rather than produce it.
STORAGE_PATTERNS = ("water tank", "water pit", "TES", "thermal energy storage", "storage")

#: Contributions below this share of the node's building heat are dropped
#: (solved networks carry 1e-6 MW dust on every unused technology).
MIN_SHARE = 1e-4

#: Bus carrier fragment identifying district heating.
DISTRICT_HEATING = "urban central heat"

#: Prefixes stripped from a carrier before it is labelled.
_PREFIXES = ("residential ", "services ", "rural ", "urban decentral ", "urban central ")

#: (fragment, decentral label, district-heating label), first match wins.
_LABELS: tuple[tuple[str, str, str], ...] = (
    ("ground heat pump", "Ground-source heat pump", "Large ground-source heat pump"),
    ("air heat pump", "Air-source heat pump", "Large air-source heat pump"),
    ("water heat pump", "Water-source heat pump", "Large water-source heat pump"),
    ("heat pump", "Heat pump", "Large heat pump"),
    ("micro gas CHP", "Gas micro-CHP", "Gas micro-CHP"),
    ("gas CHP CC", "Gas CHP with capture", "Gas CHP with capture"),
    ("gas CHP", "Gas CHP", "Gas CHP"),
    ("solid biomass CHP CC", "Biomass CHP with capture", "Biomass CHP with capture"),
    ("solid biomass CHP", "Biomass CHP", "Biomass CHP"),
    ("waste CHP", "Waste CHP", "Waste CHP"),
    ("CHP", "CHP", "CHP"),
    ("gas boiler", "Gas boiler", "Gas boiler (district)"),
    ("oil boiler", "Oil boiler", "Oil boiler (district)"),
    ("biomass boiler", "Biomass boiler", "Biomass boiler (district)"),
    ("H2 boiler", "Hydrogen boiler", "Hydrogen boiler (district)"),
    ("resistive heater", "Electric resistive heater", "Electric boiler"),
    ("solar thermal", "Solar thermal", "Solar thermal (district)"),
    ("geothermal", "Geothermal", "Geothermal"),
    ("Fuel Cell", "Fuel cell", "Fuel cell"),
    ("Fischer-Tropsch", "Power-to-X waste heat", "Power-to-X waste heat"),
    ("Electrolysis", "Power-to-X waste heat", "Power-to-X waste heat"),
    ("methanation", "Power-to-X waste heat", "Power-to-X waste heat"),
    ("Sabatier", "Power-to-X waste heat", "Power-to-X waste heat"),
    ("methanolisation", "Power-to-X waste heat", "Power-to-X waste heat"),
    ("Haber-Bosch", "Power-to-X waste heat", "Power-to-X waste heat"),
)

#: Display label -> key in ``tech_colors.csv`` (resolved with fallbacks).
COLOR_KEYS: dict[str, str] = {
    "Ground-source heat pump": "ground heat pump",
    "Air-source heat pump": "air heat pump",
    "Water-source heat pump": "heat pump",
    "Heat pump": "heat pump",
    "Large ground-source heat pump": "ground heat pump",
    "Large air-source heat pump": "air heat pump",
    "Large water-source heat pump": "heat pump",
    "Large heat pump": "heat pump",
    "Gas micro-CHP": "CHP",
    "Gas CHP": "CHP",
    "Gas CHP with capture": "#4d3b6e",
    "Biomass CHP": "biomass CHP",
    "Biomass CHP with capture": "biomass CHP CC",
    "Gas boiler": "gas boiler",
    "Gas boiler (district)": "gas boiler",
    "Oil boiler": "oil boiler",
    "Oil boiler (district)": "oil boiler",
    "Biomass boiler": "biomass boiler",
    "Biomass boiler (district)": "biomass boiler",
    "Electric resistive heater": "#b5e37d",
    "Electric boiler": "#6fae2f",
    "Solar thermal": "solar thermal",
    "Solar thermal (district)": "solar thermal",
    "District heating": "district heating",
}


def heat_color(label: str, palette: dict[str, str]) -> str:
    """Colour of a heat label: an explicit ``#rrggbb`` or a palette key."""
    from ..charts.base import resolve_tech_color

    key = COLOR_KEYS.get(label, label)
    if str(key).startswith("#"):
        return str(key)
    return resolve_tech_color(key, palette)


@dataclass
class HeatSplit:
    """Residential/tertiary heat of one node and horizon, TWh."""

    #: Technology -> heat delivered to buildings by decentral systems.
    decentral: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    #: Heat delivered to buildings through district heating.
    district: float = 0.0
    #: Technology -> district-heating heat delivered to buildings.
    district_mix: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    #: District-heating output that goes to other users (industry, DAC, ...).
    district_other: float = 0.0

    @property
    def total(self) -> float:
        return float(self.decentral.sum()) + float(self.district)


def heat_label(carrier: str, district: bool) -> str:
    """Display label of a heat-supplying carrier."""
    name = str(carrier)
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    for fragment, decentral_label, district_label in _LABELS:
        if fragment in name:
            return district_label if district else decentral_label
    return name[:1].upper() + name[1:]


def _is_storage(carrier: str) -> bool:
    return any(p in str(carrier) for p in STORAGE_PATTERNS)


def _is_building_load(carrier: str) -> bool:
    return not any(p in str(carrier) for p in NON_BUILDING_LOADS)


def _bus_terms(n, bus: str, weights: pd.Series) -> tuple[pd.Series, float, float]:
    """(supply by carrier, building load, other withdrawals), MWh/a, one bus."""
    supply: dict[str, float] = {}
    other = 0.0

    links = n.links
    for column in [c for c in links.columns if c.startswith("bus") and c[3:].isdigit()]:
        port = column[3:]
        attached = links.index[links[column] == bus]
        if not len(attached):
            continue
        flows = getattr(n.links_t, f"p{port}", None)
        if flows is None:
            continue
        cols = attached.intersection(flows.columns)
        if not len(cols):
            continue
        energy = -(flows[cols].mul(weights, axis=0).sum())
        for name, value in energy.items():
            carrier = str(links.at[name, "carrier"])
            if _is_storage(carrier):
                continue
            if value >= 0:
                supply[carrier] = supply.get(carrier, 0.0) + float(value)
            else:
                other += -float(value)

    gens = n.generators.index[n.generators.bus == bus]
    if len(gens):
        energy = n.generators_t.p.reindex(columns=gens, fill_value=0.0).mul(weights, axis=0).sum()
        for name, value in energy.items():
            carrier = str(n.generators.at[name, "carrier"])
            if value >= 0:
                supply[carrier] = supply.get(carrier, 0.0) + float(value)
            else:
                other += -float(value)

    building = 0.0
    loads = n.loads.index[n.loads.bus == bus]
    if len(loads):
        static = n.loads.loc[loads]
        energy = n.loads_t.p.reindex(columns=loads)
        # Static (time-invariant) loads have no column in loads_t.p.
        missing = energy.columns[energy.isna().all()]
        if len(missing):
            energy[missing] = static.loc[missing, "p_set"].to_numpy()
        energy = energy.fillna(0.0).mul(weights, axis=0).sum()
        for name, value in energy.items():
            if _is_building_load(str(static.at[name, "carrier"])):
                building += float(value)
            else:
                other += float(value)
    return pd.Series(supply, dtype=float), building, other


def heat_split(ctx: BuildContext, node: str, horizon: int) -> HeatSplit | None:
    """Residential/tertiary heat of ``node`` in ``horizon``, by technology."""
    try:
        n = ctx.networks[horizon]
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    if "location" not in n.buses.columns:
        return None
    locations = ctx.locations_for(node)
    buses = n.buses
    heat = buses.index[buses.carrier.astype(str).str.contains("heat", na=False)]
    if locations is not None:
        heat = heat[buses.loc[heat, "location"].astype(str).isin([str(x) for x in locations])]
    if not len(heat):
        return None
    weights = n.snapshot_weightings.generators

    decentral: dict[str, float] = {}
    mix: dict[str, float] = {}
    district = 0.0
    district_other = 0.0
    for bus in heat:
        is_district = DISTRICT_HEATING in str(buses.at[bus, "carrier"])
        supply, building, other = _bus_terms(n, bus, weights)
        produced = float(supply.sum())
        if is_district:
            district_other += other
        if produced <= 0 or building <= 0:
            continue
        delivered = supply / produced * building
        if is_district:
            district += building
            target = mix
        else:
            target = decentral
        for carrier, value in delivered.items():
            label = heat_label(carrier, is_district)
            target[label] = target.get(label, 0.0) + float(value)

    if not decentral and not district:
        return None
    to_twh = 1e-6

    floor = MIN_SHARE * (sum(decentral.values()) + district) * to_twh

    def order(values: dict[str, float]) -> pd.Series:
        series = pd.Series(values, dtype=float) * to_twh
        return series.loc[series.abs() >= floor].sort_values(ascending=False)

    return HeatSplit(
        decentral=order(decentral),
        district=district * to_twh,
        district_mix=order(mix),
        district_other=district_other * to_twh,
    )


# ---------------------------------------------------------------------------
# District-heating bus: hourly balance and storage level
# ---------------------------------------------------------------------------

#: Labels of the non-producing columns of the district-heating balance.
_DH_FLOW_LABELS: tuple[tuple[str, str], ...] = (
    # "discharger" first: it also contains "charger".
    ("discharger", "Thermal storage: discharging"),
    ("charger", "Thermal storage: charging"),
    ("low-temperature heat for industry", "Industrial low-temperature heat"),
    ("heat vent", "Heat vented"),
    ("DAC", "Direct air capture"),
    ("urban central heat", "Heat demand (buildings)"),
)

COLOR_KEYS.update({
    # The palette's hot-water entries are three near-identical salmon tints,
    # unreadable next to the heat demand: storage gets two blues instead.
    "Thermal storage: charging": "#9ecae1",
    "Thermal storage: discharging": "#3182bd",
    "Heat demand (buildings)": "heat demand",
    "Industrial low-temperature heat": "low-temperature heat for industry",
    "Heat vented": "heat vent",
})


def district_heat_label(carrier: str) -> str:
    """Display label of a column of the district-heating balance."""
    name = str(carrier)
    for fragment, label in _DH_FLOW_LABELS:
        if fragment in name and (fragment != "urban central heat" or name == fragment):
            return label
    return heat_label(name, district=True)


def district_heat_balance(
    ctx: BuildContext,
    node: str,
    horizon: int,
    start=None,
    stop=None,
    *,
    min_mw: float = 5.0,
) -> pd.DataFrame | None:
    """Hourly balance of the node's district-heating bus(es), MW, + = supply.

    Unlike the regional heat dispatch, columns keep the distinctions that
    matter on a heat network: CHP with and without capture, electric boilers,
    large heat pumps, and the charging and discharging of thermal storage.
    """
    from .balance import _branch_flows, _one_port_flows

    try:
        n = ctx.networks[horizon]
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    buses = n.buses.index[n.buses.carrier.astype(str).str.contains(DISTRICT_HEATING, na=False)]
    locations = ctx.locations_for(node)
    if locations is not None and "location" in n.buses.columns:
        buses = buses[n.buses.loc[buses, "location"].astype(str).isin([str(x) for x in locations])]
    if not len(buses):
        return None
    resolver = ctx.resolver(horizon)
    # The bus set is already the node's; select every component on it.
    flows = _branch_flows(n, resolver, buses, None)
    flows = _one_port_flows(n, resolver, buses, None, flows)
    if flows.empty:
        return None
    flows.columns = [district_heat_label(c) for c in flows.columns]
    flows = flows.T.groupby(level=0, sort=False).sum().T
    flows = flows.loc[:, (flows.abs() >= min_mw).any()]
    if start is not None or stop is not None:
        flows = flows.loc[start:stop]
    return None if flows.empty else flows


def district_heat_storage_level(ctx: BuildContext, node: str, horizon: int,
                                start=None, stop=None) -> pd.Series | None:
    """Energy in the district-heating stores (pits, tanks), MWh, or ``None``."""
    try:
        n = ctx.networks[horizon]
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    stores = n.stores
    if stores.empty or "location" not in n.buses.columns:
        return None
    mask = stores.carrier.astype(str).str.contains("urban central water", na=False)
    locations = ctx.locations_for(node)
    if locations is not None:
        mask &= stores.bus.map(n.buses.location).astype(str).isin([str(x) for x in locations])
    names = stores.index[mask]
    if not len(names) or "e" not in n.stores_t or n.stores_t.e.empty:
        return None
    level = n.stores_t.e.reindex(columns=names, fill_value=0.0).sum(axis=1)
    if start is not None or stop is not None:
        level = level.loc[start:stop]
    return None if (level.abs() < 1.0).all() else level
