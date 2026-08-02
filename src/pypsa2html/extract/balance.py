"""Nodal energy balance time series for dispatch charts.

Ports the ~110-line shared prologue of ``plot_series_power``,
``plot_series_heat`` and ``Dispatch_plots_weekly.plot_series_*`` into one
extractor.  Values are returned in MW; charts convert to GW.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..charts.base import apply_tech_map, normalize_carrier
from ..config import DispatchWindowsConfig
from ..context import BuildContext

logger = logging.getLogger(__name__)

#: Drop technologies whose entire series stays below this level (MW).
_DISPATCH_THRESHOLD_MW = 100.0  # 0.1 GW — applied consistently before display scaling

#: Pipelines dropped from dispatch views (legacy dropped both in ``Pypsa_results``
#: but only H2 in ``Dispatch_plots_weekly``).
_DROP_GROUPS = frozenset({"H2 pipeline", "gas pipeline"})

#: Default calendar anchors when ``dispatch_windows`` is null (month, start day).
_SEASON_ANCHORS = {"winter": (2, 8), "summer": (7, 1)}


def dispatch_window(
    ctx: BuildContext,
    season: str,
    *,
    snapshots: pd.DatetimeIndex | None = None,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Return ``(start, stop)`` for a dispatch week, from config or snapshots."""
    windows = getattr(ctx.config.model, "dispatch_windows", None) or DispatchWindowsConfig()
    configured = getattr(windows, season, None)
    if configured:
        if len(configured) != 2:
            logger.warning(
                "dispatch_windows.%s must be [start, stop], got %r", season, configured
            )
            return None
        start = pd.Timestamp(configured[0])
        stop = pd.Timestamp(configured[1])
        return start, stop

    if snapshots is None and hasattr(ctx, "networks"):
        try:
            snapshots = pd.DatetimeIndex(ctx.networks.first().snapshots)
        except (FileNotFoundError, KeyError, IndexError, AttributeError):
            return None
    if snapshots is None:
        return None
    return derive_dispatch_window(snapshots, season)


def derive_dispatch_window(
    snapshots: pd.DatetimeIndex,
    season: str,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Pick a representative week near February or July from ``snapshots``."""
    if season not in _SEASON_ANCHORS:
        raise ValueError(f"season must be one of {sorted(_SEASON_ANCHORS)}, got {season!r}")
    idx = pd.DatetimeIndex(snapshots)
    if idx.empty:
        return None
    month, day = _SEASON_ANCHORS[season]
    year = int(idx[0].year)
    target = pd.Timestamp(year=year, month=month, day=day)
    pos = int(idx.searchsorted(target, side="left"))
    if pos >= len(idx):
        pos = len(idx) - 1
    start = idx[pos]
    stop = start + pd.Timedelta(days=6)
    if stop > idx[-1]:
        stop = idx[-1]
    return start, stop


def energy_balance(
    ctx: BuildContext,
    node: str,
    carrier: str,
    horizon: int,
    start: pd.Timestamp | str | None = None,
    stop: pd.Timestamp | str | None = None,
) -> pd.DataFrame | None:
    """Nodal supply/demand balance for one carrier and horizon (MW, tech groups)."""
    if horizon not in ctx.networks:
        logger.warning("no network for horizon %s", horizon)
        return None
    try:
        n = ctx.networks[horizon]
    except FileNotFoundError:
        logger.warning("solved network missing for horizon %s", horizon)
        return None

    if not len(n.snapshots):
        logger.warning("network for horizon %s has no snapshots", horizon)
        return None

    buses = n.buses.index[n.buses.carrier.str.contains(carrier, na=False)]
    if buses.empty:
        logger.warning(
            "no buses matching carrier %r for horizon %s", carrier, horizon
        )
        return None

    aggregate = ctx.is_aggregate(node)
    resolver = ctx.resolver(horizon)
    supply = _branch_flows(n, resolver, node, buses, aggregate)
    supply = _one_port_flows(n, resolver, node, buses, aggregate, supply)

    if carrier == "AC":
        imp_exp = _import_export(n, resolver, node, aggregate)
        supply = supply.assign(Imports_Exports=imp_exp)

    if carrier == "heat":
        supply = supply.rename(
            columns={"urban central resistive heater": "centralised electric boiler"}
        )

    supply = _group_by_tech(supply)

    if carrier == "AC":
        supply = _add_curtailment(n, resolver, node, aggregate, supply)
        supply = _add_v2g(n, resolver, node, aggregate, supply)

    supply = supply.drop(columns=[c for c in supply.columns if c in _DROP_GROUPS], errors="ignore")

    if carrier == "heat":
        supply = _heat_column_cleanup(supply)

    supply = _drop_small_columns(supply)

    if start is not None or stop is not None:
        supply = supply.loc[start:stop]

    if supply.empty or not len(supply.columns):
        logger.warning(
            "empty energy balance for node %s carrier %r horizon %s",
            node,
            carrier,
            horizon,
        )
        return None
    return supply


#: ``iterate_components`` names → :class:`NodeResolver` component keys.
_BRANCH_COMPONENT = {"Link": "links", "Line": "lines", "Transformer": "transformers"}
_ONE_PORT_COMPONENT = {
    "Generator": "generators",
    "Load": "loads",
    "StorageUnit": "storage_units",
    "Store": "stores",
    "ShuntImpedance": "shunt_impedances",
}


def _branch_flows(n, resolver, node: str, buses: pd.Index, aggregate: bool) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for component in n.iterate_components(n.branch_components):
        n_port = 4 if component.name == "Link" else 2
        static = component.df
        comp_key = _BRANCH_COMPONENT.get(component.name, f"{component.name.lower()}s")
        for port in range(n_port):
            key = f"p{port}"
            if key not in component.pnl:
                continue
            bus_col = f"bus{port}"
            connected = static.index[static[bus_col].isin(buses)]
            if not len(connected):
                continue
            if aggregate:
                sel = connected
            else:
                node_mask = resolver.mask(comp_key, node, bus_attr=bus_col)
                sel = connected.intersection(static.index[node_mask])
            if not len(sel):
                continue
            pnl = (-1) * component.pnl[key].loc[:, sel]
            parts.append(pnl.T.groupby(static.loc[sel, "carrier"]).sum().T)
    if not parts:
        return pd.DataFrame(index=n.snapshots)
    return pd.concat(parts, axis=1)


def _one_port_flows(
    n, resolver, node: str, buses: pd.Index, aggregate: bool, supply: pd.DataFrame
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = [supply] if len(supply.columns) else []
    for component in n.iterate_components(n.one_port_components):
        static = component.df
        comp_key = _ONE_PORT_COMPONENT.get(component.name, f"{component.name.lower()}s")
        connected = static.index[static.bus.isin(buses)]
        if not len(connected):
            continue
        if aggregate:
            sel = connected
        else:
            node_mask = resolver.mask(comp_key, node)
            sel = connected.intersection(static.index[node_mask])
        if not len(sel):
            continue
        pnl = component.pnl["p"].loc[:, sel].multiply(static.loc[sel, "sign"])
        parts.append(pnl.T.groupby(static.loc[sel, "carrier"]).sum().T)
    if not parts:
        return supply if len(supply.columns) else pd.DataFrame(index=n.snapshots)
    if len(parts) == 1:
        return parts[0]
    return pd.concat(parts, axis=1)


def _import_export(n, resolver, node: str, aggregate: bool) -> pd.Series:
    if aggregate:
        ac_out = n.lines_t.p0.sum(axis=1)
        ac_in = n.lines_t.p1.sum(axis=1)
        dc = n.links.carrier == "DC"
        dc_out = n.links_t.p0.loc[:, dc].sum(axis=1)
        dc_in = n.links_t.p1.loc[:, dc].sum(axis=1)
    else:
        bus0 = resolver.bus_nodes(n.lines.bus0)
        bus1 = resolver.bus_nodes(n.lines.bus1)
        ac_out = n.lines_t.p0.loc[:, bus0 == node].sum(axis=1)
        ac_in = n.lines_t.p1.loc[:, bus1 == node].sum(axis=1)
        dc_links = n.links.index[n.links.carrier == "DC"]
        dc_bus0 = resolver.bus_nodes(n.links.loc[dc_links, "bus0"])
        dc_bus1 = resolver.bus_nodes(n.links.loc[dc_links, "bus1"])
        dc_out = n.links_t.p0.loc[:, dc_links[dc_bus0 == node]].sum(axis=1)
        dc_in = n.links_t.p1.loc[:, dc_links[dc_bus1 == node]].sum(axis=1)
    merged = pd.concat([ac_out, ac_in, dc_out, dc_in], axis=1)
    return -merged.sum(axis=1)


def _group_by_tech(supply: pd.DataFrame) -> pd.DataFrame:
    if supply.empty:
        return supply
    mapped = apply_tech_map(pd.Series(supply.columns, index=supply.columns), "dispatch")
    out = supply.copy()
    out.columns = mapped.values
    if out.columns.duplicated().any():
        out = out.T.groupby(level=0, sort=False).sum().T
    return out


def _add_curtailment(
    n, resolver, node: str, aggregate: bool, supply: pd.DataFrame
) -> pd.DataFrame:
    out = supply.copy()
    for kind, supply_key, curtail_key in (
        ("solar", "solar", "solar curtailment"),
        ("onwind", "onshore wind", "onshore curtailment"),
        ("offwind", "offshore wind", "offshore curtailment"),
    ):
        curtail = _renewable_curtailment(n, resolver, node, aggregate, kind)
        if curtail is None:
            continue
        if supply_key in out.columns:
            out[supply_key] = out[supply_key] + curtail
        else:
            out[supply_key] = curtail
        out[curtail_key] = -curtail.abs()
    return out


def _renewable_curtailment(
    n, resolver, node: str, aggregate: bool, kind: str
) -> pd.Series | None:
    gens = n.generators.index[n.generators.index.str.contains(kind, case=False)]
    if not aggregate:
        node_gens = resolver.select("generators", node)
        gens = gens.intersection(node_gens)
    if not len(gens):
        return None
    available = n.generators_t.p_max_pu[gens].multiply(n.generators.p_nom_opt[gens], axis=1)
    return (available - n.generators_t.p[gens]).sum(axis=1)


def _add_v2g(n, resolver, node: str, aggregate: bool, supply: pd.DataFrame) -> pd.DataFrame:
    if "V2G" not in n.carriers.index:
        return supply
    v2g_links = n.links.index[n.links.carrier == "V2G"]
    if not len(v2g_links):
        return supply
    if aggregate:
        v2g = n.links_t.p1.loc[:, v2g_links].sum(axis=1)
    else:
        node_mask = resolver.mask("links", node, bus_attr="bus1")
        sel = v2g_links.intersection(n.links.index[node_mask])
        if not len(sel):
            return supply
        v2g = n.links_t.p1.loc[:, sel].sum(axis=1)
    out = supply.copy()
    grid_key = "electricity distribution grid"
    if grid_key not in out.columns:
        out[grid_key] = 0.0
    out[grid_key] = out[grid_key] + v2g
    out["V2G"] = v2g.abs()
    return out


def _heat_column_cleanup(supply: pd.DataFrame) -> pd.DataFrame:
    out = supply.copy()
    out = out.rename(columns={"electricity": "electric demand", "heat": "heat demand"})
    out.columns = [normalize_carrier(str(c)) for c in out.columns]
    if out.columns.duplicated().any():
        out = out.T.groupby(level=0, sort=False).sum().T
    return out


def _drop_small_columns(supply: pd.DataFrame) -> pd.DataFrame:
    if supply.empty:
        return supply
    keep = supply.columns[(supply.abs() >= _DISPATCH_THRESHOLD_MW).any()]
    return supply.loc[:, keep]
