"""Cost, capacity and demand tables from PyPSA-Eur ``csvs/nodal_*.csv``.

Replaces the legacy ``costs`` / ``capacities`` / ``plot_demands`` pipeline in
``Pypsa_results.py``.  Horizons are read from the ``planning_horizon`` header
row (the original renamed columns positionally, which forced pypsa-wal to
alias its 2025 horizon as ``2020``).  Node membership uses the ``location``
column exactly -- never ``.str[:2]`` or ``.filter(like=)``.
"""

from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from ..charts.base import OMIT_GROUP, apply_tech_map, omit_carriers
from ..context import BuildContext
from ..datafiles import load_taxonomy
from .capacity_filter import (
    capacity_keys_from_network,
    electric_output_scaling,
    heat_output_scaling,
)

logger = logging.getLogger(__name__)

CostKind = Literal["total", "capital", "marginal", "clustered"]
CapacityKind = Literal["power", "storage", "ccs"]
#: Capacities view of capture plant, rated on the fuel-input side (MW), not MW_e
#: and not MtCO₂.  ``CCGT CC`` stays ungrouped so it remains a sibling of CCGT
#: on the power panel.
_CCS_CAPACITY_CARRIERS = frozenset(
    {
        "solid biomass for industry CC",
        "gas for industry CC",
        "process emissions CC",
        "SMR CC",
    }
)

#: Carriers dropped from nodal tables when transmission is attributed separately.
_TRANSMISSION_CARRIERS = frozenset({"AC", "DC"})

#: Display renames for energy-storage Stores (after the bus-carrier filter).
_STORAGE_RENAMES = {
    "urban central water pits": "Thermal Energy Storage",
    "urban central water tanks": "Thermal Energy Storage",
    "rural water tanks": "Thermal Energy Storage",
    "urban decentral water tanks": "Thermal Energy Storage",
    "battery": "Grid-scale battery",
    "home battery": "Grid-scale battery",
    "gas": "Gas storage",
}

#: Energy-flow codes -> (demand group, sector label) for sectoral demands.
#: Port of the ``mapping`` / ``mapping_eu`` dicts in ``plot_demands``.
_DEMAND_CODES: dict[str, tuple[str, str]] = {
    "preshydcfind": ("hydrogen", "hydrogen for industry"),
    "preshydcfneind": ("Non-energy", "H2 for non-energy"),
    "preshydwati": ("hydrogen", "shipping hydrogen"),
    "preslqfcffrewati": ("oil", "shipping oil"),
    "preselccfagr": ("electricity", "agriculture electricity"),
    "presvapcfagr": ("heat", "agriculture heat"),
    "prespetcfagr": ("oil", "agriculture oil"),
    "preselccfres": ("electricity", "electricity demand of residential and tertairy"),
    "presgazcfind": ("methane", "gas for Industry"),
    "presgazcfindd": ("methane", "gas for Industry"),
    "preselccfind": ("electricity", "electricity for Industry"),
    "preslqfcfavi": ("oil", "aviation oil demand"),
    "preselccftra": ("electricity", "land transport EV"),
    "preshydcftra": ("hydrogen", "land transport hydrogen demand"),
    "preslqfcftra": ("oil", "oil to transport demand"),
    "presvapcfind": ("heat", "low-temperature heat for industry"),
    "prespetcfneind": ("Non-energy", "naphtha for non-energy"),
    "preserail": ("electricity", "electricity demand for rail network"),
    "presvapcfdhs": ("heat", "Residential and tertiary DH demand"),
    # The three residential/tertiary heat buses each emit their own code in
    # carrier_flows_energy.csv: rural -> demandheat, urban decentral ->
    # demandheatc, urban central -> presvapcfdhs (plotted as DH above).
    # ``demandheatc`` was missing here until 2026-08-29, which silently dropped
    # the *largest* block: 42.0 of 59.2 TWh of Flemish residential/tertiary heat
    # in 2040 (71 %).  Because the dropped block shrinks while district heating
    # grows, the chart showed Flemish heat demand *rising* 17.2 -> 21.6 TWh
    # while the true total *fell* 59.2 -> 52.1 TWh.  See
    # pypsa-wal docs/temporary_improvement_plans.md item 7.
    "demandheat": ("heat", "Residential and tertiary heat demand"),
    "demandheatc": ("heat", "Residential and tertiary heat demand"),
    # Emitted by other reference models, kept so their reports still resolve.
    "demandheata": ("heat", "Residential and tertiary heat demand"),
    "demandheatb": ("heat", "Residential and tertiary heat demand"),
    "demandheats": ("heat", "Residential and tertiary heat demand"),
    "presenccfind": ("solid biomass", "solid biomass for Industry"),
    "presenccfindd": ("solid biomass", "solid biomass for Industry"),
    "preammind": ("hydrogen", "NH3"),
    "prespetcfind": ("oil", "Oil for industry"),
}


# ---------------------------------------------------------------------------
# Nodal CSV parsing
# ---------------------------------------------------------------------------

def parse_nodal_csv(raw: pd.DataFrame) -> pd.DataFrame:
    """Parse a PyPSA-Eur multi-header nodal CSV into a flat frame.

    Returns columns for every meta field named on the header row (``cost``,
    ``component``, ``location``, ``carrier``, …) plus one numeric column per
    planning horizon, labelled with the year string from the
    ``planning_horizon`` row.

    Horizons come from that header — never a positional rename to
    2020/2030/2040/2050 (SEPIA C6).
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    labels = raw.iloc[:, 0].astype(str)
    ph_hits = labels[labels == "planning_horizon"]
    if ph_hits.empty:
        raise ValueError("nodal CSV has no 'planning_horizon' header row")
    ph_idx = int(ph_hits.index[0])
    header_idx = ph_idx + 1
    if header_idx >= len(raw):
        raise ValueError("nodal CSV truncated after planning_horizon row")

    year_cols: list[int] = []
    years: list[str] = []
    for j in range(raw.shape[1]):
        val = raw.iat[ph_idx, j]
        if pd.isna(val):
            continue
        text = str(val).strip()
        if not text or text == "planning_horizon":
            continue
        try:
            years.append(str(int(float(text))))
            year_cols.append(j)
        except ValueError:
            continue
    if not years:
        raise ValueError("nodal CSV planning_horizon row has no year columns")

    meta_cols: list[tuple[int, str]] = []
    for j in range(raw.shape[1]):
        if j in year_cols:
            continue
        name = raw.iat[header_idx, j]
        if pd.isna(name) or str(name).strip() == "":
            continue
        meta_cols.append((j, str(name).strip()))

    body = raw.iloc[header_idx + 1 :].copy()
    out = pd.DataFrame({name: body.iloc[:, j].to_numpy() for j, name in meta_cols})
    for year, j in zip(years, year_cols, strict=True):
        out[year] = pd.to_numeric(body.iloc[:, j].to_numpy(), errors="coerce")
    return out.reset_index(drop=True)


def _load_nodal(ctx: BuildContext, relpath: str) -> pd.DataFrame | None:
    """Cached parse of a nodal CSV under ``results/``."""
    key = ("nodal_table", relpath)
    if key in ctx._files:
        cached = ctx._files[key]
        return None if cached is None else cached.copy()

    raw = ctx.read_csv(relpath, base="results", header=None)
    if raw is None:
        ctx._files[key] = None
        return None
    try:
        parsed = parse_nodal_csv(raw)
    except ValueError as exc:
        logger.warning("could not parse %s: %s", relpath, exc)
        ctx._files[key] = None
        return None
    ctx._files[key] = parsed
    return parsed.copy()


def _node_locations(ctx: BuildContext, node: str) -> list[str] | None:
    """Locations to keep in a nodal CSV. ``None`` = no filter (study-wide)."""
    if hasattr(ctx, "locations_for"):
        return ctx.locations_for(node)
    if hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node):
        return None
    return [str(node)]


def _filter_location(df: pd.DataFrame, ctx: BuildContext, node: str) -> pd.DataFrame:
    if "location" not in df.columns:
        logger.warning("nodal table has no 'location' column")
        return df.iloc[0:0]
    locations = _node_locations(ctx, node)
    if locations is None:
        return df
    loc = df["location"].astype(str)
    return df.loc[loc.isin([str(c) for c in locations])]


#: Default |€/MWh| above which a bus price is treated as an empty-bus dual.
_DEFAULT_PRICE_ABS_CAP = 1.0e4


def sanitize_prices(
    values: pd.Series,
    *,
    cap: float | None = None,
) -> pd.Series:
    """Replace non-finite and |price| > ``cap`` with NaN (SEPIA B3).

    Empty buses (e.g. an unused methanol bus) produce shadow prices of order
    1e5–1e6 €/MWh.  Prices are not plotted yet; call this before they are.
    ``cap`` defaults to ``1e4`` €/MWh.
    """
    numeric = pd.to_numeric(values, errors="coerce")
    limit = _DEFAULT_PRICE_ABS_CAP if cap is None else float(cap)
    return numeric.where(numeric.abs() <= limit)


def _transmission_enabled(ctx: BuildContext) -> bool:
    """Whether AC/DC transmission rows should be attributed separately.

    ``features.transmission_costs: null`` (default) means auto-on when the
    model has more than one real node -- meaningful EU-wide, noise for a
    single-region study.
    """
    flag = ctx.config.features.transmission_costs
    if flag is True:
        return True
    if flag is False:
        return False
    return len(ctx.nodes.real_codes) > 1


def _capacity_filter_mode(ctx: BuildContext) -> str:
    """``bus_carrier`` (default) or ``off`` — see ``features.capacity_filter``."""
    raw = ctx.config.raw.get("features", {}) or {}
    mode = raw.get("capacity_filter", ctx.config.features.capacity_filter)
    if mode is None:
        return "bus_carrier"
    return str(mode)


def _capacity_filter_sets(
    ctx: BuildContext,
) -> tuple[frozenset[tuple[str, str]] | None, frozenset[str] | None]:
    """``(power_keys, storage_carriers)`` or ``(None, None)`` when filter is off.

    ``power_keys`` are ``(component, carrier)`` pairs.  Cached on ``ctx``.
    """
    if _capacity_filter_mode(ctx) == "off":
        return None, None

    cache_key = "_capacity_filter_sets"
    cached = getattr(ctx, cache_key, None)
    if cached is None:
        try:
            n = ctx.networks.first()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "capacity filter: cannot load a network (%s); "
                "falling back to optional tech_groups __omit__ only",
                exc,
            )
            cached = (None, None)
        else:
            cached = capacity_keys_from_network(n)
            logger.info(
                "capacity filter: %d power keys / %d storage carriers from network",
                len(cached[0]),
                len(cached[1]),
            )
        setattr(ctx, cache_key, cached)
    return cached


def _apply_capacity_filter(
    df: pd.DataFrame, ctx: BuildContext, kind: CapacityKind
) -> pd.DataFrame:
    """Restrict ``df`` to topology-approved component/carrier pairs."""
    power_keys, storage_carriers = _capacity_filter_sets(ctx)
    if kind == "storage":
        if storage_carriers is None or "carrier" not in df.columns:
            return df
        return df.loc[df["carrier"].astype(str).isin(storage_carriers)]
    if kind == "ccs":
        if "carrier" not in df.columns:
            return df
        return df.loc[df["carrier"].astype(str).isin(_CCS_CAPACITY_CARRIERS)]
    if power_keys is None:
        return df
    if "carrier" not in df.columns:
        return df
    if "component" in df.columns:
        keys = pd.Series(
            list(
                zip(
                    df["component"].astype(str),
                    df["carrier"].astype(str),
                    strict=False,
                )
            ),
            index=df.index,
        )
        return df.loc[keys.isin(power_keys)]
    # No component column: keep any carrier that appears in at least one key.
    carriers = {c for _comp, c in power_keys}
    return df.loc[df["carrier"].astype(str).isin(carriers)]


def _group_techs(df: pd.DataFrame, years: list[str], view: str) -> pd.DataFrame:
    """Map carriers to display groups and sum."""
    if df.empty or "carrier" not in df.columns:
        return pd.DataFrame(columns=years)
    work = df.copy()
    # Optional name denylist in tech_groups.csv (group ``__omit__``).
    if "carrier" in work.columns:
        work = work.loc[~omit_carriers(work["carrier"].astype(str), view)]
    if work.empty:
        return pd.DataFrame(columns=years)
    work = work.copy()
    work["tech"] = apply_tech_map(work["carrier"].astype(str), view)
    work = work.loc[work["tech"] != OMIT_GROUP]
    if work.empty:
        return pd.DataFrame(columns=years)
    grouped = work.groupby("tech", sort=False)[years].sum()
    # Drop all-zero / non-finite rows (load shedding is often +inf).
    grouped = grouped.replace([float("inf"), float("-inf")], float("nan")).dropna(how="any")
    return grouped.loc[~(grouped == 0).all(axis=1)]


def _merge_base_year(
    table: pd.DataFrame,
    ctx: BuildContext,
    node: str,
    *,
    relpath: str,
    tech_col: str = "tech",
) -> pd.DataFrame:
    """Optionally prepend an exogenous base-year column from country_csvs."""
    base = ctx.config.model.base_year
    if base is None:
        return table
    base_s = str(base)
    hist = ctx.read_csv(relpath, base="results")
    if hist is None:
        logger.warning("base_year=%s set but %s missing", base, relpath)
        return table
    hist = hist.copy()
    if tech_col in hist.columns:
        hist = hist.set_index(tech_col)
    elif hist.index.name in (None, tech_col) or "tech" in str(hist.columns[0]).lower():
        # already indexed, or first column is tech
        if hist.index.name is None and hist.columns[0] in ("tech", "Unnamed: 0"):
            hist = hist.set_index(hist.columns[0])
    # Find a column to use as the base year (legacy used "2020" then renamed)
    year_col = None
    for candidate in (base_s, "2020", "2019"):
        if candidate in hist.columns:
            year_col = candidate
            break
    if year_col is None:
        logger.warning("no base-year column in %s", relpath)
        return table

    series = pd.to_numeric(hist[year_col], errors="coerce").fillna(0.0)
    series.index = apply_tech_map(pd.Series(series.index.astype(str)), "costs").values
    series = series.groupby(level=0).sum()

    out = table.copy()
    if base_s not in out.columns:
        # Insert at the left if it's in year_columns
        cols = [c for c in ctx.year_columns if c == base_s or c in out.columns]
        out = out.reindex(columns=cols, fill_value=0.0)
    out[base_s] = series.reindex(out.index).fillna(0.0)
    # Techs only in history
    missing = series.index.difference(out.index)
    if len(missing):
        extra = pd.DataFrame(0.0, index=missing, columns=out.columns)
        extra[base_s] = series.loc[missing]
        out = pd.concat([out, extra])
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cost_table(
    ctx: BuildContext, node: str, kind: CostKind = "total"
) -> pd.DataFrame | None:
    """Technology costs for ``node``. Index=tech, columns=``ctx.year_columns``."""
    raw = _load_nodal(ctx, "csvs/nodal_costs.csv")
    if raw is None:
        return None

    df = _filter_location(raw, ctx, node)
    if df.empty:
        logger.warning("cost_table: no rows for node %s", node)
        return None

    years = list(ctx.year_columns)
    # Drop exogenous base year from the solved-horizon slice; merge later.
    solved = [y for y in years if y in df.columns]
    if not solved:
        # year_columns may include base_year not present in nodal CSV
        solved = [c for c in df.columns if str(c).isdigit()]
        if not solved:
            logger.warning("cost_table: no year columns for node %s", node)
            return None

    if kind in ("capital", "marginal") and "cost" in df.columns:
        df = df.loc[df["cost"].astype(str) == kind]
    elif kind == "total" and "cost" in df.columns:
        pass  # sum all cost types
    elif kind == "clustered":
        pass
    else:
        if kind in ("capital", "marginal"):
            logger.warning("cost_table: no 'cost' column; cannot filter kind=%s", kind)
            return None

    if _transmission_enabled(ctx):
        df = df.loc[~df["carrier"].astype(str).isin(_TRANSMISSION_CARRIERS)]

    view = "clustered" if kind == "clustered" else "costs"
    # For clustered, first map to costs groups then to clustered buckets.
    if kind == "clustered":
        mid = _group_techs(df, solved, "costs")
        if mid.empty:
            return None
        mapped = apply_tech_map(pd.Series(mid.index.astype(str)), "clustered")
        mid = mid.copy()
        mid.index = mapped.values
        table = mid.groupby(level=0).sum()
        table = table.loc[~(table == 0).all(axis=1)]
    else:
        table = _group_techs(df, solved, view)

    if table.empty:
        return None

    table = table.reindex(columns=list(ctx.year_columns), fill_value=0.0)

    if kind != "clustered" and ctx.config.model.base_year is not None:
        table = _merge_base_year(
            table, ctx, node, relpath=f"country_csvs/costs_{node}.csv"
        )
        table = table.reindex(columns=list(ctx.year_columns), fill_value=0.0)

    return table.sort_index()


def _nuclear_electric_mw(ctx: BuildContext, node: str) -> pd.Series:
    """Electric nuclear capacity [MW] per horizon, attributed by plant bus.

    ``nodal_capacities.csv`` parks every nuclear Link under location ``EU``
    (the uranium bus).  The legacy reporter instead took
    ``p_nom_opt * efficiency`` on links whose *delivery* bus sits in the
    selected node.  Replicate that here so country panels are not blank.
    """
    years = [str(y) for y in ctx.year_columns]
    out = pd.Series(0.0, index=years, dtype=float)
    if not hasattr(ctx, "networks") or not hasattr(ctx, "horizons"):
        return out

    locations = _node_locations(ctx, node)
    for horizon in ctx.horizons:
        try:
            network = ctx.networks[horizon]
        except Exception:  # noqa: BLE001 - missing / stub networks are fine
            continue
        links = getattr(network, "links", None)
        if links is None or getattr(links, "empty", True):
            continue
        if "carrier" not in links.columns or "p_nom_opt" not in links.columns:
            continue
        nuc = links.loc[links["carrier"].astype(str) == "nuclear"]
        if nuc.empty:
            continue
        if locations is not None:
            buses = getattr(network, "buses", None)
            if buses is not None and "location" in buses.columns:
                dest = nuc["bus1"].map(buses["location"])
            else:
                dest = nuc["bus1"].astype(str)
            nuc = nuc.loc[dest.astype(str).isin([str(c) for c in locations])]
        if nuc.empty:
            continue
        eff = (
            pd.to_numeric(nuc["efficiency"], errors="coerce").fillna(1.0)
            if "efficiency" in nuc.columns
            else 1.0
        )
        p_nom = pd.to_numeric(nuc["p_nom_opt"], errors="coerce").fillna(0.0)
        out[str(horizon)] = float((p_nom * eff).sum())
    return out


def _p2h_scaling(ctx: BuildContext) -> dict[str, float]:
    """Cached ``{carrier: factor}`` restating power-to-heat p_nom as MW_th."""
    cache_key = "_p2h_heat_scaling"
    cached = getattr(ctx, cache_key, None)
    if cached is None:
        try:
            n = ctx.networks.first()
        except Exception as exc:  # noqa: BLE001 - never break a build over this
            logger.warning(
                "power-to-heat scaling: cannot load a network (%s); capacities "
                "will mix MW_th (heat pumps) with MW_e (resistive heaters)",
                exc,
            )
            cached = {}
        else:
            cached = heat_output_scaling(n)
            scaled = {k: v for k, v in cached.items() if v != 1.0}
            if scaled:
                logger.info(
                    "power-to-heat: restating %d carrier(s) on the heat side %s",
                    len(scaled),
                    {k: round(v, 3) for k, v in scaled.items()},
                )
        setattr(ctx, cache_key, cached)
    return cached


def _capacity_rescaling(ctx: BuildContext) -> dict[str, dict[str, float]]:
    """``{year: {carrier: factor}}`` restating Link p_nom, per horizon.

    Both maps put a Link on the side of the service it delivers — electricity
    for power plants, heat for power-to-heat — so one bar can be compared with
    the next.  They are disjoint by construction
    (:func:`~pypsa2html.extract.capacity_filter.electric_output_scaling` is
    given the power-to-heat carriers as its ``exclude`` set) and power-to-heat
    wins if that ever changes, because a heat pump's ``p_nom`` is already MW_th
    and that is the rating wanted for it.

    Factors are read **per horizon**: a carrier's fleet efficiency moves as new
    vintages arrive (CCGT 0.570 in 2025 -> 0.588 in 2050), and reusing the
    first horizon's value would misstate the last one by ~3 %.  Year columns
    with no horizon of their own (an exogenous ``base_year``) fall back to the
    earliest map.
    """
    cache_key = "_capacity_rescaling_by_year"
    cached = getattr(ctx, cache_key, None)
    if cached is not None:
        return cached

    p2h = _p2h_scaling(ctx)
    by_year: dict[str, dict[str, float]] = {}
    for horizon in getattr(ctx, "horizons", []):
        try:
            n = ctx.networks[horizon]
        except Exception as exc:  # noqa: BLE001 - never break a build over this
            logger.warning(
                "electric rating: no network for horizon %s (%s); thermal "
                "plants stay on their fuel side (MW_th) in that column",
                horizon,
                exc,
            )
            continue
        by_year[str(horizon)] = {**electric_output_scaling(n, exclude=p2h), **p2h}

    if by_year:
        first = by_year[min(by_year)]
        electric = {k: v for k, v in first.items() if k not in p2h}
        logger.info(
            "electric rating: restating %d Link carrier(s) as MW_e %s",
            len(electric),
            {k: round(v, 3) for k, v in sorted(electric.items())},
        )
    else:
        logger.warning(
            "electric rating: no network available; thermal power plants will "
            "be plotted on their fuel side (MW_th)"
        )
    setattr(ctx, cache_key, by_year)
    return by_year


def _apply_capacity_rescaling(
    df: pd.DataFrame, ctx: BuildContext, years: list[str]
) -> pd.DataFrame:
    """Restate Link ``p_nom`` on the side of the service the Link delivers.

    Two corrections, both needed before capacities from different Links can
    share an axis:

    * **power plants -> MW_e.** PyPSA rates ``p_nom`` at ``bus0``, which for a
      CCGT, a CHP or a nuclear plant is the *fuel* bus, so the raw value is
      MW_th — a CCGT bar 1.75x its plate rating, incomparable with the wind
      Generator beside it.  See
      :func:`pypsa2html.extract.capacity_filter.electric_output_scaling`.
    * **power-to-heat -> MW_th.** Heat pumps are already thermal; resistive
      heaters are not.  See
      :func:`pypsa2html.extract.capacity_filter.heat_output_scaling`.
    """
    by_year = _capacity_rescaling(ctx)
    if not by_year or df.empty or "carrier" not in df.columns:
        return df
    fallback = by_year[min(by_year)]
    out = df.copy()
    carriers = out["carrier"].astype(str)
    for year in years:
        factors = by_year.get(str(year), fallback)
        for carrier, factor in factors.items():
            if factor == 1.0:
                continue
            hit = carriers == carrier
            if hit.any():
                out.loc[hit, year] = out.loc[hit, year] * factor
    return out


def capacity_table(
    ctx: BuildContext, node: str, kind: CapacityKind = "power"
) -> pd.DataFrame | None:
    """Installed capacities for ``node``. Power in MW; storage in MWh.

    By default (``features.capacity_filter: bus_carrier``) only carriers that
    attach to electricity / heat / H2 service buses — or energy-storage Store
    buses — are kept.  See :mod:`pypsa2html.extract.capacity_filter`.

    Nuclear is re-attributed from the solved network (delivery bus ×
    efficiency) because the nodal CSV stores it under the EU uranium bus.
    """
    raw = _load_nodal(ctx, "csvs/nodal_capacities.csv")
    if raw is None:
        return None

    df = _filter_location(raw, ctx, node)
    if df.empty and kind == "storage":
        logger.warning("capacity_table: no rows for node %s", node)
        return None

    if kind == "storage":
        if df.empty:
            return None
        if "component" in df.columns:
            df = df.loc[df["component"].astype(str) == "Store"]
        else:
            logger.warning("capacity_table: no component column for storage filter")
            return None
    elif kind == "ccs":
        if df.empty:
            return None
        if "component" in df.columns:
            df = df.loc[df["component"].astype(str) == "Link"]
        if "carrier" in df.columns:
            df = df.loc[df["carrier"].astype(str).isin(_CCS_CAPACITY_CARRIERS)]
    elif kind == "power":
        # Keep AC/DC so ``normalize_carrier`` folds them into
        # "transmission lines" for the faceted capacity chart.  Cost tables
        # still drop them when ``features.transmission_costs`` is on.
        # Exclude pure Store energy capacities from the power chart
        if not df.empty and "component" in df.columns:
            df = df.loc[df["component"].astype(str) != "Store"]
        # Drop CSV nuclear — replaced below from the network.
        if not df.empty and "carrier" in df.columns:
            df = df.loc[df["carrier"].astype(str) != "nuclear"]

    df = _apply_capacity_filter(df, ctx, kind) if not df.empty else df

    years_in_csv = (
        [c for c in df.columns if str(c).isdigit()] if not df.empty else []
    )
    if not years_in_csv and kind == "storage":
        return None

    if kind == "storage":
        if df.empty:
            return None
        work = df.copy()
        carriers = work["carrier"].astype(str)
        techs = []
        for carrier in carriers:
            if carrier in _STORAGE_RENAMES:
                techs.append(_STORAGE_RENAMES[carrier])
            else:
                techs.append(apply_tech_map(pd.Series([carrier]), "capacities").iloc[0])
        work["tech"] = techs
        table = work.groupby("tech", sort=False)[years_in_csv].sum()
        table = table.replace([float("inf"), float("-inf")], float("nan")).dropna(how="any")
    else:
        if df.empty or not years_in_csv:
            table = pd.DataFrame(columns=list(ctx.year_columns))
        else:
            df = _apply_capacity_rescaling(df, ctx, years_in_csv)
            table = _group_techs(df, years_in_csv, "capacities")

    table = table.reindex(columns=list(ctx.year_columns), fill_value=0.0)

    if kind == "power":
        nuclear = _nuclear_electric_mw(ctx, node)
        if nuclear.any():
            table = table.copy()
            if "nuclear" in table.index:
                table = table.drop(index="nuclear")
            table.loc["nuclear"] = nuclear.reindex(table.columns).fillna(0.0)

    table = table.loc[~(table == 0).all(axis=1)]
    if table.empty:
        return None

    if kind == "power" and ctx.config.model.base_year is not None:
        table = _merge_base_year(
            table, ctx, node, relpath=f"country_csvs/capacities_{node}.csv"
        )
        table = table.reindex(columns=list(ctx.year_columns), fill_value=0.0)

    return table.sort_index()


# ---------------------------------------------------------------------------
# Utilisation (capacity factors / storage cycles)
# ---------------------------------------------------------------------------

UtilisationKind = Literal["power", "storage"]

#: How a throughput is read per component: ``(static attr, capacity attr,
#: time-series attr, port, sign rule)``.
#:
#: ``Link`` reads **p0** — the port PyPSA rates ``p_nom`` at, whichever side of
#: the link that happens to be (``bus0`` is gas for a CCGT, electricity for an
#: electrolyser and *heat* for a heat pump, which is a reversed link).  Taking
#: numerator and denominator on the same port is what makes the factor mean the
#: same thing for all of them, and it also makes the heat-side rescaling of
#: :func:`~pypsa2html.extract.capacity_filter.heat_output_scaling` irrelevant
#: here: the factor would appear in both terms and cancel.
#:
#: ``abs`` is for branches, which can flow either way; ``positive`` is for
#: components whose negative half is a *different* operating mode that must not
#: be added to the dispatch (a charging StorageUnit or Store).
_UTILISATION_PORTS: dict[str, tuple[str, str, str, str, str]] = {
    "Generator": ("generators", "p_nom_opt", "generators_t", "p", "positive"),
    "Link": ("links", "p_nom_opt", "links_t", "p0", "abs"),
    "StorageUnit": ("storage_units", "p_nom_opt", "storage_units_t", "p", "positive"),
    "Line": ("lines", "s_nom_opt", "lines_t", "p0", "abs"),
}

#: Same, for the storage view: energy *leaving* the store over ``e_nom_opt``.
_UTILISATION_STORE = ("stores", "e_nom_opt", "stores_t", "p", "positive")


def _snapshot_weights(n, component: str) -> pd.Series:
    """Hours represented by each snapshot, for ``component``'s energy sum.

    A 6h-resolution run carries weight 6, so summing raw MW would understate
    the energy by the resolution — and the capacity factor with it.
    """
    snapshots = pd.Index(n.snapshots)
    weightings = getattr(n, "snapshot_weightings", None)
    if weightings is None or getattr(weightings, "empty", True):
        return pd.Series(1.0, index=snapshots)
    preferred = ("stores",) if component == "Store" else ("generators", "objective")
    for column in (*preferred, "objective", "generators"):
        if column in weightings.columns:
            values = pd.to_numeric(weightings[column], errors="coerce")
            return values.reindex(snapshots).fillna(1.0)
    return pd.Series(1.0, index=snapshots)


def _throughput(n, spec: tuple[str, str, str, str, str], index: pd.Index,
                weights: pd.Series) -> pd.Series:
    """Energy [MWh] through ``spec``'s rated port, per component name."""
    _static_attr, _cap_attr, ts_attr, port, sign = spec
    frame = getattr(getattr(n, ts_attr, None), port, None)
    if frame is None or getattr(frame, "empty", True):
        return pd.Series(0.0, index=index, dtype=float)
    columns = frame.columns.intersection(index)
    if columns.empty:
        return pd.Series(0.0, index=index, dtype=float)
    values = pd.DataFrame(frame[columns]).apply(pd.to_numeric, errors="coerce").fillna(0.0)
    values = values.abs() if sign == "abs" else values.clip(lower=0.0)
    energy = values.mul(weights.reindex(values.index).fillna(1.0), axis=0).sum()
    return energy.reindex(index).fillna(0.0)


def _utilisation_terms(
    ctx: BuildContext,
    node: str,
    horizon: int,
    kind: UtilisationKind,
) -> tuple[pd.Series, pd.Series, float] | None:
    """``(energy, capacity, hours)`` per technology group for one horizon."""
    try:
        n = ctx.networks[horizon]
    except Exception as exc:  # noqa: BLE001 - a missing horizon is not fatal
        logger.warning("utilisation: no network for horizon %s (%s)", horizon, exc)
        return None
    if not len(n.snapshots):
        logger.warning("utilisation: network for horizon %s has no snapshots", horizon)
        return None

    power_keys, storage_carriers = _capacity_filter_sets(ctx)
    specs = (
        {"Store": _UTILISATION_STORE}
        if kind == "storage"
        else dict(_UTILISATION_PORTS)
    )

    energy_parts: list[pd.Series] = []
    capacity_parts: list[pd.Series] = []
    hours = 0.0

    for component, spec in specs.items():
        static_attr, cap_attr, _ts_attr, _port, _sign = spec
        static = getattr(n, static_attr, None)
        if static is None or getattr(static, "empty", True):
            continue
        if "carrier" not in static.columns or cap_attr not in static.columns:
            continue

        index = ctx.component_index(horizon, static_attr, node)
        if len(index) == 0:
            continue
        rows = static.loc[static.index.intersection(index)]
        carriers = rows["carrier"].astype(str)

        # Same admission rules as ``capacity_table``: the topology filter, then
        # the ``__omit__`` denylist, then the display grouping.
        if kind == "storage":
            if storage_carriers is not None:
                rows = rows.loc[carriers.isin(storage_carriers)]
        elif power_keys is not None:
            keep = carriers.map(lambda c, comp=component: (comp, c) in power_keys)
            rows = rows.loc[keep]
        if rows.empty:
            continue

        carriers = rows["carrier"].astype(str)
        rows = rows.loc[~omit_carriers(carriers, "capacities")]
        if rows.empty:
            continue
        carriers = rows["carrier"].astype(str)
        if kind == "storage":
            techs = carriers.map(
                lambda c: _STORAGE_RENAMES.get(
                    c, apply_tech_map(pd.Series([c]), "capacities").iloc[0]
                )
            )
        else:
            techs = apply_tech_map(carriers, "capacities")
        rows = rows.loc[techs != OMIT_GROUP]
        techs = techs.loc[rows.index]
        if rows.empty:
            continue

        weights = _snapshot_weights(n, component)
        hours = max(hours, float(weights.sum()))
        energy = _throughput(n, spec, rows.index, weights)
        capacity = pd.to_numeric(rows[cap_attr], errors="coerce").fillna(0.0)

        energy_parts.append(energy.groupby(techs).sum())
        capacity_parts.append(capacity.groupby(techs).sum())

    if not energy_parts:
        return None
    energy_total = pd.concat(energy_parts).groupby(level=0).sum()
    capacity_total = pd.concat(capacity_parts).groupby(level=0).sum()
    return energy_total, capacity_total, hours


def utilisation_table(
    ctx: BuildContext, node: str, kind: UtilisationKind = "power"
) -> pd.DataFrame | None:
    """Utilisation of the installed fleet for ``node``, per technology group.

    ``power`` returns a **capacity factor** in p.u.: the energy through the
    rated port over ``p_nom_opt`` × the hours the horizon represents.
    ``storage`` returns the **equivalent full cycles per year**: the energy
    discharged over ``e_nom_opt``.

    Rows are the technology groups :func:`capacity_table` uses, so a factor
    reads directly against the capacity bar above it.  A group without
    capacity in a horizon is ``NaN``, never ``0``: there is no utilisation to
    report, and a zero bar would be read as an idle fleet.

    Computed from the solved networks rather than ``csvs/nodal_capacities.csv``
    because PyPSA-Eur exports no capacity factor for Links — which is every
    conversion technology (electrolysis, Fischer-Tropsch, CCGT, heat pumps).
    """
    if not hasattr(ctx, "networks") or not hasattr(ctx, "horizons"):
        return None

    energy = pd.DataFrame(dtype=float)
    capacity = pd.DataFrame(dtype=float)
    hours: dict[str, float] = {}

    for horizon in ctx.horizons:
        terms = _utilisation_terms(ctx, node, horizon, kind)
        if terms is None:
            continue
        year = str(horizon)
        energy_h, capacity_h, hours_h = terms
        energy = energy.reindex(energy.index.union(energy_h.index))
        capacity = capacity.reindex(capacity.index.union(capacity_h.index))
        energy[year] = energy_h.reindex(energy.index)
        capacity[year] = capacity_h.reindex(capacity.index)
        hours[year] = hours_h

    if energy.empty:
        logger.warning("utilisation_table: nothing to report for node %s", node)
        return None

    index = energy.index.union(capacity.index)
    energy = energy.reindex(index).fillna(0.0)
    capacity = capacity.reindex(index).fillna(0.0)

    denominator = capacity.copy()
    if kind == "power":
        for year in denominator.columns:
            denominator[year] = denominator[year] * hours.get(year, 0.0)
    table = energy.where(denominator > 0.0) / denominator.where(denominator > 0.0)

    table = table.reindex(columns=list(ctx.year_columns))
    table = table.loc[table.notna().any(axis=1)]
    if table.empty:
        return None
    table.attrs["energy_mwh"] = energy
    table.attrs["capacity"] = capacity
    return table.sort_index()

def _warn_unmapped_demand_codes() -> None:
    """Warn when ``carrier_flows_energy.csv`` emits a demand code we never plot.

    The sectoral-demand chart is a *whitelist*: only codes in
    :data:`_DEMAND_CODES` reach it, and a code that is emitted but not listed
    disappears from the chart without any error.  That is how the whole
    ``urban decentral heat`` block went missing.  Any row whose label says
    "demand" is meant to be plotted, so flag the mismatch loudly.
    """
    try:
        flows = load_taxonomy().carrier_flows_energy
    except Exception as exc:  # noqa: BLE001 - never break a build over a check
        logger.debug("demand-code check skipped: %s", exc)
        return

    labelled = flows.loc[
        flows["label"].astype(str).str.contains("demand", case=False, na=False)
    ]
    # ``entry`` names ending in ``_2``/``_3`` are secondary *ports* of a link
    # ("kerosene for aviation_2" -> "aviation oil demand to demand"). They carry
    # the same energy as the primary row under a second code, so plotting them
    # would double-count. The bug this guard exists for is a missing *bus*
    # entry, which never has a port suffix. Heuristic, not a proof.
    primary = labelled.loc[
        ~labelled["entry"].astype(str).str.contains(r"_\d+$", regex=True, na=False)
    ]
    missing = sorted(
        {
            str(code)
            for code in primary["code"]
            if str(code) and str(code) not in _DEMAND_CODES
        }
    )
    if missing:
        logger.warning(
            "carrier_flows_energy.csv emits demand code(s) %s that are absent "
            "from _DEMAND_CODES; they will be missing from the sectoral-demand "
            "chart. Add them to pypsa2html.extract.tables._DEMAND_CODES.",
            missing,
        )


def demand_table(ctx: BuildContext, node: str) -> pd.DataFrame | None:
    """Sectoral final-energy demands. Index=sector label, columns=years.

    Built from the energy-flow extraction when a network is available; returns
    ``None`` when flows cannot be produced.
    """
    from .flows import energy_flows

    _warn_unmapped_demand_codes()

    try:
        flows = energy_flows(ctx, node)
    except Exception as exc:  # noqa: BLE001 - builders must not raise
        logger.warning("demand_table: energy_flows failed for %s: %s", node, exc)
        return None
    if flows is None or flows.empty:
        return None

    years = [y for y in ctx.year_columns if y in flows.columns]
    if not years:
        return None

    rows = []
    for code, (group, sector) in _DEMAND_CODES.items():
        hit = flows.loc[flows["code"] == code]
        if hit.empty:
            continue
        vals = hit[years].sum()
        rows.append({"group": group, "sector": sector, **vals.to_dict()})

    if not rows:
        logger.warning("demand_table: no demand codes matched for node %s", node)
        return None

    out = pd.DataFrame(rows).groupby("sector", sort=False)[years].sum()
    out = out.reindex(columns=list(ctx.year_columns), fill_value=0.0)
    out.attrs["groups"] = (
        pd.DataFrame(rows).drop_duplicates("sector").set_index("sector")["group"]
    )
    return out.loc[~(out == 0).all(axis=1)]
