"""The district-heating network told as a year, not as two weeks.

A week of hourly dispatch shows *how* the network is balanced and hides *why*.
Three things that decide a heat network are invisible in a week: the thermal
store fills and empties over weeks to months, electric heat runs in the few
hours when electricity is nearly free, and a heat pump that would save most of
that electricity is not built because those hours are too few to pay for it.
This module extracts what the charts in :mod:`pypsa2html.charts.district_heating`
need to say so:

* :func:`district_heat_year` — hourly supply by source, store flows and level,
  demand and the two prices that govern them (electricity and heat);
* :func:`by_price_group` — the same energy sorted by the electricity price of
  the hour, in groups of equal length;
* :func:`electric_heat_value` — for each electric heating technology, what one
  MW_th would earn at the solved prices against what it costs.

**Prices are duals.**  The heat price is the marginal price of the
district-heating bus: the cost of one more MWh_th in that hour.  It is not a
tariff.  The operating margin of a technology is the heat price minus the cost
of the electricity it needs, summed over the hours where that is positive, per
MW of capacity.  A link is built until the last MW earns exactly its annualised
capital cost, so a technology whose margin is below its capital cost is not
built — and one built at the margin earns its capital cost to the euro, which
makes the resistive heater a built-in check of the whole calculation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..context import BuildContext
from .capacity_filter import _P2H_PATTERNS, heat_output_scaling
from .heat import DISTRICT_HEATING, district_heat_balance
from .tables import sanitize_prices

logger = logging.getLogger(__name__)

#: Labels of the storage columns of :func:`~pypsa2html.extract.heat.district_heat_balance`.
CHARGING = "Thermal storage: charging"
DISCHARGING = "Thermal storage: discharging"

#: Carriers whose stores are the district-heating thermal storage.
_STORE_FRAGMENT = "urban central water"

#: Producers contributing less than this share of the network's heat are dropped
#: (solved networks carry 1e-6 MW of dust on every unused technology).
MIN_SHARE = 1e-4

#: Bus carriers that are electricity.
_ELECTRIC_BUS_CARRIERS = ("low voltage", "AC")

#: Heat-price classes of the investment test, EUR/MWh_th: upper edges.  They
#: split the year into hours when heat is almost free, cheap, dear and scarce.
HEAT_PRICE_EDGES = (5.0, 20.0, 80.0)

#: Number of electricity-price groups of :func:`by_price_group`.
PRICE_GROUPS = 10

#: Capacity below this (MW_th) is numerical dust, not a plant.
_DUST_MW = 1e-3


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def band_labels(edges: tuple[float, ...] = HEAT_PRICE_EDGES) -> list[str]:
    """``["≤ 5", "5–20", "20–80", "> 80"]`` for the default edges."""
    def fmt(x: float) -> str:
        return f"{x:g}"

    labels = [f"≤ {fmt(edges[0])}"]
    labels += [f"{fmt(a)}–{fmt(b)}" for a, b in zip(edges[:-1], edges[1:])]
    labels.append(f"> {fmt(edges[-1])}")
    return labels


def band_of(price: pd.Series, edges: tuple[float, ...] = HEAT_PRICE_EDGES) -> pd.Series:
    """Class number (0 .. len(edges)) of every price; NaN prices stay NaN."""
    idx = np.searchsorted(np.asarray(edges, dtype=float), price.to_numpy(dtype=float), side="left")
    out = pd.Series(idx.astype(float), index=price.index)
    return out.where(price.notna())


def daily_mean(data: pd.Series | pd.DataFrame, weights: pd.Series):
    """Mean over each calendar day, weighted by the hours a snapshot stands for."""
    w = weights.reindex(data.index).fillna(1.0)
    if isinstance(data, pd.Series):
        num = (data * w).resample("D").sum()
        den = w.resample("D").sum()
        return num / den.where(den > 0)
    num = data.mul(w, axis=0).resample("D").sum()
    den = w.resample("D").sum()
    return num.div(den.where(den > 0), axis=0)


def district_buses(ctx: BuildContext, n, node: str) -> pd.Index:
    """District-heating buses of ``node`` (every member's, for a group)."""
    buses = n.buses.index[n.buses.carrier.astype(str).str.contains(DISTRICT_HEATING, na=False)]
    locations = ctx.locations_for(node)
    if locations is not None and "location" in n.buses.columns:
        buses = buses[n.buses.loc[buses, "location"].astype(str).isin([str(x) for x in locations])]
    return buses


def _price_cap(ctx: BuildContext) -> float | None:
    return getattr(getattr(ctx.config, "features", None), "price_abs_cap", None)


def _prices(ctx: BuildContext, n) -> pd.DataFrame | None:
    """Sanitised bus marginal prices, or ``None`` when the network has none."""
    prices = getattr(n.buses_t, "marginal_price", None)
    if prices is None or getattr(prices, "empty", True):
        return None
    cap = _price_cap(ctx)
    return prices.apply(lambda col: sanitize_prices(col, cap=cap))


def _electric_bus(n, heat_bus: str) -> str | None:
    """Electricity bus that feeds the electric heating plant of ``heat_bus``.

    Read off the power-to-heat links attached to the bus, so that the price is
    the one the plant pays (``low voltage`` in PyPSA-Eur's distribution-grid
    model, ``AC`` without it).  Falls back to the bus of that kind in the same
    location.
    """
    links = n.links
    attached = links.index[(links.bus0 == heat_bus) | (links.bus1 == heat_bus)]
    carriers = links.loc[attached, "carrier"].astype(str).str.lower()
    for name in attached[carriers.map(lambda c: any(p in c for p in _P2H_PATTERNS)).to_numpy()]:
        for column in ("bus0", "bus1"):
            other = links.at[name, column]
            if other != heat_bus and n.buses.at[other, "carrier"] in _ELECTRIC_BUS_CARRIERS:
                return str(other)
    if "location" in n.buses.columns:
        here = n.buses.at[heat_bus, "location"]
        for carrier in _ELECTRIC_BUS_CARRIERS:
            match = n.buses.index[(n.buses.carrier == carrier) & (n.buses.location == here)]
            if len(match):
                return str(match[0])
    return None


# ---------------------------------------------------------------------------
# the year
# ---------------------------------------------------------------------------

@dataclass
class DistrictHeat:
    """The district-heating network of one node and horizon, hourly."""

    horizon: int
    buses: list[str]
    #: Hours each snapshot stands for.
    weights: pd.Series
    #: MW delivered to the network, by source (>= 0).
    producers: pd.DataFrame
    #: MW taken into / released from the thermal store (>= 0).
    charging: pd.Series
    discharging: pd.Series
    #: MW taken by every user of the network (buildings, industry, vent, ...).
    demand: pd.Series
    #: Energy in the store, MWh (``None`` without a store).
    soc: pd.Series | None = None
    soc_capacity: float | None = None
    #: Energy that left the store over the year / its capacity.
    store_cycles: float | None = None
    #: EUR/MWh_th and EUR/MWh_el; ``None`` for a group of regions (prices of
    #: different regions must not be averaged into one).
    heat_price: pd.Series | None = None
    electricity_price: pd.Series | None = None
    single_region: bool = True

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.producers.index

    @property
    def annual_demand_twh(self) -> float:
        return float(self.demand.mul(self.weights).sum()) * 1e-6


def district_heat_year(ctx: BuildContext, node: str, horizon: int) -> DistrictHeat | None:
    """Hourly network, store and prices of ``node`` in ``horizon`` (cached)."""
    files = getattr(ctx, "_files", None)
    key = ("district_heat_year", str(node), int(horizon))
    if files is not None and key in files:
        return files[key]
    result = _build_year(ctx, node, horizon)
    if files is not None:
        files[key] = result
    return result


def _build_year(ctx: BuildContext, node: str, horizon: int) -> DistrictHeat | None:
    try:
        n = ctx.networks[horizon]
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    buses = district_buses(ctx, n, node)
    if not len(buses):
        return None
    balance = district_heat_balance(ctx, node, horizon, min_mw=0.0)
    if balance is None or balance.empty:
        return None
    if not isinstance(balance.index, pd.DatetimeIndex):
        try:
            balance.index = pd.DatetimeIndex(balance.index)
        except (TypeError, ValueError):
            logger.warning("district heating: snapshots are not dates, no yearly view")
            return None
    weights = n.snapshot_weightings.generators.reindex(balance.index).fillna(1.0)

    zero = pd.Series(0.0, index=balance.index)
    charging = (-balance.get(CHARGING, zero)).clip(lower=0.0)
    discharging = balance.get(DISCHARGING, zero).clip(lower=0.0)
    flows = balance.drop(columns=[c for c in (CHARGING, DISCHARGING) if c in balance.columns])
    producers = flows.clip(lower=0.0)
    demand = (-flows).clip(lower=0.0).sum(axis=1)

    energy = producers.mul(weights, axis=0).sum()
    producers = producers.loc[:, energy > MIN_SHARE * max(float(energy.sum()), 1e-9)]
    if producers.empty and float(demand.sum()) <= 0:
        return None

    soc, capacity, cycles = _store(ctx, n, node, weights)
    heat_price = electricity_price = None
    single = len(buses) == 1
    if single:
        prices = _prices(ctx, n)
        if prices is not None and buses[0] in prices.columns:
            heat_price = prices[buses[0]]
            electric = _electric_bus(n, buses[0])
            if electric is not None and electric in prices.columns:
                electricity_price = prices[electric]

    return DistrictHeat(
        horizon=int(horizon), buses=[str(b) for b in buses], weights=weights,
        producers=producers, charging=charging, discharging=discharging, demand=demand,
        soc=soc, soc_capacity=capacity, store_cycles=cycles,
        heat_price=heat_price, electricity_price=electricity_price, single_region=single,
    )


def _store(ctx: BuildContext, n, node: str, weights: pd.Series):
    """``(energy stored MWh, capacity MWh, cycles)`` of the heat-network stores."""
    stores = n.stores
    if stores.empty or "location" not in n.buses.columns:
        return None, None, None
    mask = stores.carrier.astype(str).str.contains(_STORE_FRAGMENT, na=False)
    locations = ctx.locations_for(node)
    if locations is not None:
        mask &= stores.bus.map(n.buses.location).astype(str).isin([str(x) for x in locations])
    names = stores.index[mask]
    if not len(names) or "e" not in n.stores_t or n.stores_t.e.empty:
        return None, None, None
    level = n.stores_t.e.reindex(columns=names, fill_value=0.0).sum(axis=1)
    if bool((level.abs() < 1.0).all()):
        return None, None, None
    level = level.reindex(weights.index)
    capacity = float(pd.to_numeric(stores.loc[names, "e_nom_opt"], errors="coerce").fillna(0.0).sum())
    cycles = None
    if capacity > 0 and "p" in n.stores_t and not n.stores_t.p.empty:
        out = n.stores_t.p.reindex(columns=names, fill_value=0.0).clip(lower=0.0)
        cycles = float(out.mul(weights.reindex(out.index).fillna(1.0), axis=0).sum().sum()) / capacity
    return level, (capacity if capacity > 0 else None), cycles


# ---------------------------------------------------------------------------
# energy by electricity price
# ---------------------------------------------------------------------------

@dataclass
class PriceGroups:
    """Heat energy of the year, hours sorted into groups by electricity price."""

    #: Lowest / highest electricity price in each group, EUR/MWh.
    low: np.ndarray
    high: np.ndarray
    #: Hours in each group.
    hours: np.ndarray
    #: GWh_th by group (rows) and source (columns).
    producers: pd.DataFrame
    charging: np.ndarray
    discharging: np.ndarray
    demand: np.ndarray
    #: Hours-weighted mean prices in each group.
    electricity_price: np.ndarray
    heat_price: np.ndarray | None
    #: Mean cost of one MWh_th made by each electric technology, per group.
    cost_of_heat: dict[str, np.ndarray] = field(default_factory=dict)

    def labels(self) -> list[str]:
        out = []
        for lo, hi in zip(self.low, self.high):
            out.append(f"{lo:.0f}–{hi:.0f}" if abs(hi - lo) >= 0.5 else f"{lo:.0f}")
        return out


def by_price_group(
    ctx: BuildContext, node: str, horizon: int, groups: int = PRICE_GROUPS,
) -> PriceGroups | None:
    """Sort the hours by electricity price and add the heat up in each group.

    The groups hold equal numbers of hours (a "decile" of the year), so each bar
    stands for the same stretch of time whatever the price distribution is.
    """
    dh = district_heat_year(ctx, node, horizon)
    if dh is None or dh.electricity_price is None:
        return None
    price = dh.electricity_price.reindex(dh.index)
    w = dh.weights.reindex(dh.index).fillna(1.0)
    valid = price.notna()
    if not bool(valid.any()):
        return None
    order = price[valid].sort_values(kind="mergesort").index
    cum = w.loc[order].cumsum() / float(w.loc[order].sum())
    group = pd.Series(np.minimum(((cum - 1e-12) * groups).astype(int), groups - 1), index=order)
    group = group.reindex(dh.index)

    def energy(frame):
        """MW -> GWh_th per group."""
        weighted = frame.mul(w, axis=0) if isinstance(frame, pd.DataFrame) else frame * w
        return weighted.groupby(group).sum() * 1e-3

    gid = list(range(groups))
    producers = energy(dh.producers).reindex(gid).fillna(0.0)
    pr = price.loc[order]
    low = pr.groupby(group.loc[order]).min().reindex(gid).to_numpy()
    high = pr.groupby(group.loc[order]).max().reindex(gid).to_numpy()
    hours = w.groupby(group).sum().reindex(gid).fillna(0.0).to_numpy()

    def mean_in_group(series: pd.Series) -> np.ndarray:
        num = (series * w).where(series.notna()).groupby(group).sum()
        den = w.where(series.notna()).groupby(group).sum()
        return (num / den.where(den > 0)).reindex(gid).to_numpy()

    result = PriceGroups(
        low=low, high=high, hours=hours, producers=producers,
        charging=energy(dh.charging).reindex(gid).fillna(0.0).to_numpy(),
        discharging=energy(dh.discharging).reindex(gid).fillna(0.0).to_numpy(),
        demand=energy(dh.demand).reindex(gid).fillna(0.0).to_numpy(),
        electricity_price=mean_in_group(price),
        heat_price=None if dh.heat_price is None else mean_in_group(dh.heat_price),
    )
    for candidate in _candidates(ctx, ctx.networks[horizon], dh.buses[0]):
        cost = price * candidate.electricity_per_heat.reindex(price.index)
        result.cost_of_heat[candidate.label] = mean_in_group(cost)
    return result


# ---------------------------------------------------------------------------
# is electric heat worth building?
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    """An extendable electric heating link and what it would earn."""

    carrier: str
    label: str
    link: str
    #: MWh of electricity per MWh_th, hourly (1/COP for a heat pump).
    electricity_per_heat: pd.Series
    #: MW_th per MW of ``p_nom`` (1 for a heat pump, the efficiency for a boiler).
    thermal_factor: float


@dataclass
class ElectricHeat:
    """One electric heating technology on the district-heating bus."""

    carrier: str
    label: str
    #: Installed (all vintages) and added in this horizon, MW_th.
    installed_mw_th: float
    new_mw_th: float
    heat_twh: float
    electricity_twh: float
    #: Hours of full output the installed plant runs per year.
    full_load_hours: float
    #: Annualised capital cost, EUR per MW_th and year.
    capital_cost: float
    #: Operating margin by heat-price class, EUR per MW_th and year.
    margin_bands: pd.Series
    #: Hours in which one more MW would earn something.
    hours_positive: float
    #: Mean price of the electricity the installed plant actually bought, EUR/MWh.
    electricity_price_paid: float | None = None

    @property
    def margin(self) -> float:
        return float(self.margin_bands.sum())

    @property
    def coverage(self) -> float:
        """Margin over capital cost: 1 = built at the margin, < 1 = not worth building."""
        return self.margin / self.capital_cost if self.capital_cost > 0 else float("nan")

    @property
    def cop(self) -> float | None:
        return self.heat_twh / self.electricity_twh if self.electricity_twh > 0 else None


def _link_series(n, link: str, attr: str) -> pd.Series:
    """A link attribute as an hourly series: its time series, else its static value."""
    dynamic = getattr(n.links_t, attr, None)
    if dynamic is not None and link in dynamic.columns:
        return pd.to_numeric(dynamic[link], errors="coerce").reindex(n.snapshots)
    value = pd.to_numeric(pd.Series([n.links.at[link, attr]]), errors="coerce").iloc[0]
    return pd.Series(float(value), index=n.snapshots)


def _candidates(ctx: BuildContext, n, heat_bus: str) -> list[_Candidate]:
    """Extendable electric heating links of the bus, one per technology.

    Only two-port links qualify (a heat pump with a heat-source bus, or a CHP,
    has more terms in its value than this module models).  The link of the
    current horizon's vintage is the one whose investment decision the solve
    made; an older vintage is the fallback.
    """
    from .heat import heat_label

    links = n.links
    scaling = heat_output_scaling(n)
    on_bus = links.index[(links.bus0 == heat_bus) | (links.bus1 == heat_bus)]
    out: list[_Candidate] = []
    for carrier, names in links.loc[on_bus].groupby(links.loc[on_bus, "carrier"].astype(str)):
        lowered = carrier.lower()
        if not any(p in lowered for p in _P2H_PATTERNS) or carrier not in scaling:
            continue
        group = links.loc[names.index]
        if "bus2" in group.columns and group["bus2"].astype(str).str.len().gt(0).any():
            continue
        extendable = group.loc[group["p_nom_extendable"].astype(bool)] if "p_nom_extendable" in group else group
        if extendable.empty:
            continue
        if "build_year" in extendable.columns:
            built = pd.to_numeric(extendable["build_year"], errors="coerce").fillna(0.0)
            newest = extendable.index[int(built.to_numpy().argmax())]
        else:
            newest = extendable.index[0]
        row = links.loc[newest]
        eff = _link_series(n, newest, "efficiency")
        if row.bus1 == heat_bus:          # boiler: bus0 electricity in, bus1 heat out
            per_heat = 1.0 / eff.where(eff > 0)
        else:                             # reversed heat pump: bus0 heat, bus1 electricity
            per_heat = eff
        out.append(_Candidate(
            carrier=carrier, label=heat_label(carrier, district=True), link=str(newest),
            electricity_per_heat=per_heat, thermal_factor=float(scaling[carrier]),
        ))
    return out


def electric_heat_value(
    ctx: BuildContext, node: str, horizon: int,
) -> tuple[list[ElectricHeat], dict] | None:
    """Earnings against capital cost of every electric heating technology.

    Returns ``(technologies, bands)`` where ``bands`` carries what the heat-price
    classes mean in this horizon: ``labels``, hours in each and the mean heat
    price in each.  ``None`` for a group of regions, for a network without
    prices and for a node without a district-heating bus.
    """
    try:
        n = ctx.networks[horizon]
    except (FileNotFoundError, KeyError, AttributeError):
        return None
    buses = district_buses(ctx, n, node)
    if len(buses) != 1:
        return None
    heat_bus = str(buses[0])
    prices = _prices(ctx, n)
    if prices is None or heat_bus not in prices.columns:
        return None
    weights = n.snapshot_weightings.generators.reindex(n.snapshots).fillna(1.0)
    heat_price = prices[heat_bus]
    band = band_of(heat_price)
    labels = band_labels()
    classes = list(range(len(labels)))

    def per_class(series: pd.Series) -> pd.Series:
        return (series * weights).where(band.notna()).groupby(band).sum().reindex(classes).fillna(0.0)

    out: list[ElectricHeat] = []
    links = n.links
    for candidate in _candidates(ctx, n, heat_bus):
        row = links.loc[candidate.link]
        b0, b1 = str(row.bus0), str(row.bus1)
        if b0 not in prices.columns or b1 not in prices.columns:
            continue
        eff = _link_series(n, candidate.link, "efficiency")
        pmin = _link_series(n, candidate.link, "p_min_pu").fillna(0.0)
        pmax = _link_series(n, candidate.link, "p_max_pu").fillna(0.0)
        # Value of one MW of p_nom running at p0 = u * p_nom: u * (eff * price1 - price0),
        # best at one of the two bounds of u.
        gain = eff * prices[b1] - prices[b0]
        mc = float(pd.to_numeric(pd.Series([row.get("marginal_cost", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
        up = pmax * gain - mc * pmax.clip(lower=0.0)
        down = pmin * gain - mc * (-pmin).clip(lower=0.0)
        margin = pd.concat([up, down], axis=1).max(axis=1).clip(lower=0.0).fillna(0.0)
        margin = margin / candidate.thermal_factor
        bands = per_class(margin)
        bands.index = labels

        same = links.index[links.carrier.astype(str) == candidate.carrier]
        same = same[(links.loc[same, "bus0"] == heat_bus) | (links.loc[same, "bus1"] == heat_bus)]
        installed = float(links.loc[same, "p_nom_opt"].sum()) * candidate.thermal_factor
        build = pd.to_numeric(links.loc[same, "build_year"], errors="coerce")
        new = float(links.loc[same[build == int(horizon)], "p_nom_opt"].sum()) * candidate.thermal_factor
        # Solved networks carry 1e-5 MW of dust on every unbuilt technology.
        installed = installed if installed >= _DUST_MW else 0.0
        new = new if new >= _DUST_MW else 0.0
        heat_mwh, el_mwh, paid = _energy(n, same, heat_bus, weights, prices)
        if installed == 0.0:
            heat_mwh = el_mwh = 0.0
            paid = None
        flh = heat_mwh / installed if installed > 0 else 0.0
        capital = float(row.capital_cost) / candidate.thermal_factor
        out.append(ElectricHeat(
            carrier=candidate.carrier, label=candidate.label,
            installed_mw_th=installed, new_mw_th=new,
            heat_twh=heat_mwh * 1e-6, electricity_twh=el_mwh * 1e-6,
            full_load_hours=flh, capital_cost=capital, margin_bands=bands,
            hours_positive=float(weights.where(margin > 1e-9).fillna(0.0).sum()),
            electricity_price_paid=paid,
        ))
    if not out:
        return None
    info = {
        "labels": labels,
        "hours": per_class(pd.Series(1.0, index=n.snapshots)).to_numpy(),
        "mean_price": (per_class(heat_price) / per_class(pd.Series(1.0, index=n.snapshots)).replace(0, np.nan)).to_numpy(),
        "heat_bus": heat_bus,
    }
    return out, info


def _energy(n, names: pd.Index, heat_bus: str, weights: pd.Series, prices: pd.DataFrame):
    """``(MWh_th delivered to the bus, MWh_el drawn, mean electricity price paid)``."""
    heat = 0.0
    electricity = 0.0
    cost = 0.0
    for name in names:
        row = n.links.loc[name]
        for port in (0, 1):
            flow = getattr(n.links_t, f"p{port}", None)
            if flow is None or name not in flow.columns:
                continue
            series = flow[name].reindex(n.snapshots).fillna(0.0)
            bus = str(row[f"bus{port}"])
            if bus == heat_bus:
                heat += float((-series).clip(lower=0.0).mul(weights).sum())
            elif n.buses.at[bus, "carrier"] in _ELECTRIC_BUS_CARRIERS:
                draw = series.clip(lower=0.0).mul(weights)
                electricity += float(draw.sum())
                if bus in prices.columns:
                    cost += float((draw * prices[bus].fillna(0.0)).sum())
    paid = cost / electricity if electricity > 0 else None
    return heat, electricity, paid
