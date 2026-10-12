"""Nodal price statistics for the key figures.

PyPSA's bus duals are hourly marginal prices.  A key figure needs a handful of
numbers per node and horizon -- the mean a consumer pays, how spiky the year
is -- not 8 760 values, so this reduces them once per horizon for every node.

Empty buses produce duals of order 1e5-1e6 EUR/MWh; :func:`sanitize_prices`
drops them (``features.price_abs_cap``) before any statistic is taken.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .tables import sanitize_prices

logger = logging.getLogger(__name__)

#: Rows of a price table, in display order.
PRICE_ROWS = (
    "mean",
    "load_weighted_mean",
    "p05",
    "median",
    "p95",
    "hours_above_100",
    "hours_above_200",
    "hours_below_1",
)


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    mask = ~np.isnan(values)
    values, weights = values[mask], weights[mask]
    if not len(values) or weights.sum() <= 0:
        return float("nan")
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cum = np.cumsum(weights) / weights.sum()
    return float(values[min(np.searchsorted(cum, q, side="left"), len(values) - 1)])


def series_stats(
    price: pd.Series, weights: pd.Series, load: pd.Series | None = None
) -> dict[str, float]:
    """The :data:`PRICE_ROWS` statistics of one hourly price series.

    ``weights`` are snapshot weightings (hours per snapshot), so ``hours_*``
    are hours of the year.  ``load`` weights ``load_weighted_mean``; without
    it that row equals the time-weighted mean.
    """
    w = weights.reindex(price.index).fillna(1.0).to_numpy(dtype=float)
    p = price.to_numpy(dtype=float)
    valid = ~np.isnan(p)
    if not valid.any():
        return {row: float("nan") for row in PRICE_ROWS}
    mean = float(np.sum(p[valid] * w[valid]) / np.sum(w[valid]))
    if load is not None:
        lw = (load.reindex(price.index).fillna(0.0).clip(lower=0).to_numpy(dtype=float)) * w
        lw_valid = lw[valid]
        weighted = (
            float(np.sum(p[valid] * lw_valid) / lw_valid.sum()) if lw_valid.sum() > 0 else mean
        )
    else:
        weighted = mean
    return {
        "mean": mean,
        "load_weighted_mean": weighted,
        "p05": _weighted_quantile(p, w, 0.05),
        "median": _weighted_quantile(p, w, 0.5),
        "p95": _weighted_quantile(p, w, 0.95),
        "hours_above_100": float(np.sum(w[valid & (p > 100)])),
        "hours_above_200": float(np.sum(w[valid & (p > 200)])),
        "hours_below_1": float(np.sum(w[valid & (p < 1)])),
    }


def _location_load(n, buses: pd.Index, location: pd.Series) -> pd.DataFrame:
    """Hourly load (MW) at each location, summed over ``buses``.

    A consumer's price is weighted by what is drawn at its *location*, not on
    the price bus itself: in PyPSA-Eur the household and tertiary electricity
    load sits on the ``low voltage`` bus while the price is read on ``AC``.
    """
    loads = n.loads.index[n.loads.bus.isin(buses)]
    if not len(loads):
        return pd.DataFrame(index=n.snapshots)
    p = n.loads_t.p.reindex(columns=loads)
    if p.isna().all().all():
        p = n.loads_t.p_set.reindex(columns=loads).fillna(n.loads.p_set.reindex(loads))
    p = p.fillna(0.0)
    where = n.loads.bus[loads].map(location)
    return p.T.groupby(where.to_numpy()).sum().T


def price_stats(ctx, kind: str) -> dict[str, pd.DataFrame]:
    """``{node code: table}`` of price statistics on the ``kind`` buses.

    A node with several matching buses (a group) gets the load-weighted
    average price of each hour, falling back to the plain average in hours
    without load.  ``load_weighted_mean`` weights each hour by the load drawn
    at the node's locations on the ``kind`` buses (for electricity: ``AC``
    and ``low voltage``).  Computed by
    :func:`pypsa2html.metrics.horizon_pass` and cached on the context.
    """
    from ..metrics import horizon_pass

    key = ("price_stats", kind)
    if key not in ctx._files:
        horizon_pass(ctx, [f"price.{kind}"])
    return ctx._files[key]


def price_horizon(ctx, n, horizon: int, kind: str) -> dict[str, pd.Series]:
    """One horizon of :func:`price_stats`: ``{node code: Series}``."""
    from ..metrics import BALANCE_CARRIERS, PRICE_CARRIERS
    from .balance import bus_carriers

    carriers = bus_carriers(kind, PRICE_CARRIERS)
    load_carriers = bus_carriers(kind, BALANCE_CARRIERS)
    cap = getattr(getattr(ctx.config, "features", None), "price_abs_cap", None)
    mp = getattr(n.buses_t, "marginal_price", None)
    if mp is None or mp.empty:
        return {}
    buses = n.buses.index[n.buses.carrier.isin(carriers)].intersection(mp.columns)
    if buses.empty:
        return {}
    resolver = ctx.resolver(horizon)
    location = resolver.bus_nodes(pd.Series(buses, index=buses))
    weights = n.snapshot_weightings.generators
    prices = mp[buses].apply(sanitize_prices, cap=cap)
    load_buses = n.buses.index[n.buses.carrier.isin(load_carriers)]
    all_location = resolver.bus_nodes(pd.Series(n.buses.index, index=n.buses.index))
    by_location = _location_load(n, load_buses, all_location)
    out: dict[str, pd.Series] = {}
    for node in ctx.nodes:
        locations = ctx.locations_for(node.code)
        if locations is None:
            sel = buses[~location.isin(["", "EU"]).to_numpy()]
        else:
            sel = buses[location.isin(locations).to_numpy()]
        if sel.empty:
            continue
        p = prices[sel]
        load = pd.DataFrame(
            {b: by_location[location[b]] if location[b] in by_location.columns
             else pd.Series(0.0, index=n.snapshots) for b in sel}
        )
        total_load = load.sum(axis=1)
        if len(sel) == 1:
            hourly = p.iloc[:, 0]
        else:
            weighted = (p * load).sum(axis=1, min_count=1) / total_load.replace(0, np.nan)
            hourly = weighted.fillna(p.mean(axis=1))
        out[node.code] = pd.Series(series_stats(hourly, weights, total_load), dtype=float)
    return out


def finalize_prices(by_year: dict[str, dict[str, pd.Series]]) -> dict[str, pd.DataFrame]:
    """``{year: {node: Series}}`` -> ``{node: PRICE_ROWS x years}``."""
    nodes: dict[str, dict[str, pd.Series]] = {}
    for year, per_node in by_year.items():
        for code, series in per_node.items():
            nodes.setdefault(code, {})[year] = series
    return {code: pd.DataFrame(cols).reindex(list(PRICE_ROWS)) for code, cols in nodes.items()}
