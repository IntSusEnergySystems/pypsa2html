"""Energy flows per node and horizon -- the port of legacy ``process_network``.

The legacy routine turned a solved network into the ``Inputs`` sheet of
``inputs<NODE>.xlsx``.  It worked, but it identified every row by a *label with
an order-dependent suffix*, mutated the cached network, and hardcoded the
country list, the year columns and the carrier->code dictionary.  This module
keeps the physics and replaces the bookkeeping.

The deterministic entry key
---------------------------
The legacy tool numbered same-carrier rows with :func:`generate_new_label`: the
first *surviving* row labelled ``CCGT`` became ``CCGT``, the second ``CCGT_2``
and so on.  "Surviving" meant *after* the ``value >= 0.1`` filter, so if a
country's CCGT fleet generated less than 0.1 TWh the transformation-losses row
was promoted into ``CCGT`` -- i.e. into the *generation* code ``proelcgaz``.
The numbering also depended on the row order of a ``pd.concat(...).sort_index()``
that interleaves six frames by positional index.

Here every candidate row carries a provenance key

    (carrier, origin, sign, port, bus0_carrier, busN_carrier)

and the suffix is the rank of that key inside its carrier, sorted by

    origin  : extra < load < link < generator < storage unit < store
    sign    : 0 for a port the link *feeds* (negative ``p``), 1 for a port it
              *draws from* (positive ``p``)
    port    : the link port index; the synthetic "losses" port sorts as the
              highest index, the synthetic heat-pump port just after it
    then the two bus carriers, so several links of one carrier that connect
    different bus carriers still get a stable order.

``rank == 1`` maps to the bare carrier name, rank *k* to ``f"{carrier}_{k}"``,
which is exactly the ``entry`` column of ``data/carrier_flows_energy.csv``.
The rank is computed over *all structurally present* rows, before any value
threshold, so a below-threshold row no longer shifts the codes of its
siblings.  Rows that are structurally impossible (``source == target``, CO2
bookkeeping ports, ...) are dropped *before* ranking, exactly as in the
original -- those drops do not depend on the values.

This reproduces the legacy assignment on the reference model for every
multi-row carrier that was checked (CCGT, methanolisation, Fischer-Tropsch,
naphtha for industry, H2 pipeline, DAC, the CHPs and the heat pumps).

Other differences from the original, all deliberate:

* nothing is written back onto ``n.links`` (the original left ``total_e*`` and
  ``carrier_bus*`` columns on the cached network, so the next call saw a
  different frame than the first one);
* node membership comes from :class:`~pypsa2html.nodes.NodeResolver` instead of
  ``.filter(like=country)``, which matched ``BE`` inside ``BEWAL``;
* the heat-pump correction is aligned on the group key rather than subtracted
  positionally with ``.values``;
* thresholding is by magnitude, so a net-negative flow survives.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..context import BuildContext

logger = logging.getLogger(__name__)

#: PyPSA works in MWh, the report in TWh.
MWH_PER_TWH = 1.0e6

#: Pseudo bus carrier for the residual of a link's energy balance.
LOSSES = "losses"

#: Sort order of the provenance origins; see the module docstring.
_ORIGIN_ORDER = {
    "extra": 0,
    "load": 1,
    "link": 2,
    "generator": 3,
    "storage_unit": 4,
    "store": 5,
}

#: Bus-carrier aggregation, applied in this order to (source, target).
#:
#: Ported verbatim from the legacy ``connections.loc[src_contains(...)]`` block.
#: It is not cosmetic: collapsing two bus carriers onto the same name is what
#: makes a row ``source == target`` and therefore dropped, so both the
#: substring semantics and the order are load-bearing.
_AGGREGATION_RULES: tuple[tuple[str, str, str], ...] = (
    ("both", "low voltage", "AC"),
    ("source", "CCGT", "gas"),
    ("target", "CCGT", "AC"),
    ("source", "OCGT", "gas"),
    ("target", "OCGT", "AC"),
    ("both", "water tank", "water tank"),
    ("source", "solar thermal", "solar thermal"),
    ("both", "battery", "battery"),
    ("both", "Li ion", "battery"),
)

#: Bus carriers that are pure CO2 bookkeeping and never carry energy.
_CO2 = "co2"

#: Source bus carriers whose *demand* row duplicates the link row feeding it.
_DUPLICATE_DEMAND_SOURCES = ("gas for industry", "solid biomass for industry")


# --------------------------------------------------------------------------
# node membership
# --------------------------------------------------------------------------
def _select(ctx: BuildContext, network, node: str, component: str, horizon: int) -> pd.Index:
    """Index of the rows of ``component`` that belong to ``node``.

    Delegates to :meth:`BuildContext.component_index` so group aggregates
    filter to ``members_of(node)`` and the study-wide aggregate stays unfiltered.
    ``network`` is unused (membership reads ``ctx.networks[horizon]``); kept so
    call sites stay stable.
    """
    return ctx.component_index(horizon, component, node)


# --------------------------------------------------------------------------
# raw flows
# --------------------------------------------------------------------------
def _link_ports(network) -> list[int]:
    """The port indices ``p0, p1, ...`` this network actually carries."""
    return sorted(
        int(key[1:])
        for key in network.links_t.keys()
        if key.startswith("p") and key[1:].isdigit()
    )


def _port_total(network, weights, port: int, index: pd.Index) -> pd.Series:
    """Snapshot-weighted annual flow at one link port, in TWh.

    Returns zeros when the port does not exist, which is the uniform guard the
    original only applied at 8 of its 18 ``p3``/``p4`` call sites.
    """
    dynamic = network.links_t
    key = f"p{port}"
    if key not in dynamic or dynamic[key].empty:
        return pd.Series(0.0, index=index)
    total = weights @ dynamic[key]
    return total.reindex(index).fillna(0.0) / MWH_PER_TWH


def _cache_get(ctx: BuildContext, key: tuple, factory):
    """Memoize ``factory()`` on ``ctx._files`` under ``key``."""
    if key not in ctx._files:
        ctx._files[key] = factory()
    return ctx._files[key]


def _link_port_totals(ctx: BuildContext, horizon: int) -> dict[int, pd.Series]:
    """Full-network link port totals in TWh — computed once per horizon.

    ``weights @ links_t.pN`` is the expensive step; every node then reindexes
    the same Series rather than repeating the matmul.
    """

    def factory() -> dict[int, pd.Series]:
        network = ctx.networks[horizon]
        weights = network.snapshot_weightings.generators
        return {
            port: _port_total(network, weights, port, network.links.index)
            for port in _link_ports(network)
        }

    return _cache_get(ctx, ("link_port_totals", horizon), factory)


def _one_port_totals(ctx: BuildContext, horizon: int, component: str) -> pd.Series:
    """Full-network annual totals for a one-port component, in TWh."""

    def factory() -> pd.Series:
        network = ctx.networks[horizon]
        weights = network.snapshot_weightings.generators
        dynamic = getattr(network, f"{component}_t")["p"]
        return (weights @ dynamic) / MWH_PER_TWH

    return _cache_get(ctx, ("one_port_totals", horizon, component), factory)


def _load_totals(ctx: BuildContext, horizon: int) -> pd.Series:
    """Full-network annual load ``p_set`` totals, in TWh."""

    def factory() -> pd.Series:
        network = ctx.networks[horizon]
        weights = network.snapshot_weightings.generators
        p_set = network.get_switchable_as_dense("Load", "p_set")
        return (weights @ p_set) / MWH_PER_TWH

    return _cache_get(ctx, ("load_totals", horizon), factory)


def _link_flows(
    network,
    index: pd.Index,
    *,
    totals: dict[int, pd.Series] | None = None,
) -> pd.DataFrame:
    """One row per (carrier, bus0 carrier, port carrier) link flow, plus losses."""
    columns = ["carrier", "source", "target", "value", "origin", "port"]
    if len(index) == 0:
        return pd.DataFrame(columns=columns)

    links = network.links.loc[index]
    ports = _link_ports(network)
    bus_carrier = {
        port: links[f"bus{port}"].map(network.buses.carrier)
        for port in ports
        if f"bus{port}" in links.columns
    }
    if totals is None:
        weights = network.snapshot_weightings.generators
        total = {port: _port_total(network, weights, port, index) for port in ports}
    else:
        total = {
            port: totals[port].reindex(index).fillna(0.0) for port in ports if port in totals
        }

    rows = []
    for port in ports[1:]:
        connected = bus_carrier.get(port)
        if connected is None:
            continue
        keep = connected.notna() & (links[f"bus{port}"].astype(str) != "")
        if not keep.any():
            continue
        rows.append(
            pd.DataFrame(
                {
                    "carrier": links["carrier"][keep],
                    "source": bus_carrier[0][keep],
                    "target": connected[keep],
                    "value": total[port][keep],
                    "origin": "link",
                    "port": port,
                }
            )
        )

    # The residual of the energy balance.  CO2 ports are excluded because they
    # are a mass flow, not energy; "process emissions" deliberately is *not*
    # excluded -- the legacy substring test only looked for "co2", and the
    # resulting (positive) residual is what feeds the `*_4` naphtha code.
    energy = pd.Series(0.0, index=index)
    for port in ports:
        connected = bus_carrier.get(port)
        if connected is None:
            continue
        is_co2 = connected.fillna("").str.contains(_CO2)
        energy = energy + total[port].where(~is_co2, 0.0)
    rows.append(
        pd.DataFrame(
            {
                "carrier": links["carrier"],
                "source": bus_carrier[0],
                "target": LOSSES,
                "value": -energy,
                "origin": "link",
                "port": max(ports) + 1,
            }
        )
    )

    flows = pd.concat(rows, ignore_index=True)
    flows = flows.dropna(subset=["source", "target"])
    return _group(flows)


def _group(flows: pd.DataFrame) -> pd.DataFrame:
    """Sum duplicate rows, keyed on everything except the value."""
    keys = [c for c in ("carrier", "source", "target", "origin", "port", "sign") if c in flows]
    return flows.groupby(keys, as_index=False, sort=True)["value"].sum()


def _heat_pump_rows(
    network,
    index: pd.Index,
    flows: pd.DataFrame,
    *,
    totals: dict[int, pd.Series] | None = None,
) -> pd.DataFrame:
    """Restate the heat-pump rows as *ambient* heat plus total heat output.

    In both reference models a heat pump link is oriented heat-first
    (``bus0`` = heat sink, ``bus1`` = electricity), so the port-1 row only
    reports the electricity drawn and the ambient contribution -- which has no
    bus of its own -- would be missing from the balance.  The original derived
    it from ``p0`` and then subtracted the two frames *positionally* with
    ``.values``; here the subtraction is aligned on the group key.

    The heat-pump losses row is dropped: with the ambient term accounted for
    there is nothing left over.
    """
    links = network.links.loc[index]
    is_pump = links["carrier"].str.contains("heat pump")
    if not is_pump.any():
        return flows

    pumps = links[is_pump]
    if totals is not None and 0 in totals:
        bus0_total = -totals[0].reindex(pumps.index).fillna(0.0)
    else:
        weights = network.snapshot_weightings.generators
        bus0_total = -_port_total(network, weights, 0, pumps.index)
    output = pd.DataFrame(
        {
            "carrier": pumps["carrier"],
            "source": pumps["bus0"].map(network.buses.carrier),
            "target": pumps["bus1"].map(network.buses.carrier),
            "value": bus0_total,
            "origin": "link",
            # sorts after every real port and after the losses port
            "port": max(_link_ports(network)) + 2,
        }
    )
    output = _group(output)

    pump_rows = flows["carrier"].str.contains("heat pump")
    flows = flows[~(pump_rows & (flows["target"] == LOSSES))].copy()

    key = ["carrier", "source", "target"]
    correction = output.set_index(key)["value"]
    pump_rows = flows["carrier"].str.contains("heat pump")
    aligned = pd.MultiIndex.from_frame(flows.loc[pump_rows, key])
    flows.loc[pump_rows, "value"] -= correction.reindex(aligned).fillna(0.0).to_numpy()

    is_air = flows["carrier"].str.contains("air heat pump")
    is_ground = flows["carrier"].str.contains("ground heat pump")
    flows.loc[is_air & pump_rows, "source"] = "air-sourced ambient"
    flows.loc[is_ground & pump_rows, "source"] = "ground-sourced ambient"

    return pd.concat([flows, output], ignore_index=True)


def _one_port_flows(
    ctx: BuildContext, network, node: str, horizon: int, component: str, origin: str
) -> pd.DataFrame:
    """Generator / store / storage-unit flows, grouped by (carrier, bus carrier)."""
    static = getattr(network, component)
    index = _select(ctx, network, node, component, horizon)
    columns = ["carrier", "source", "target", "value", "origin", "port"]
    if len(index) == 0:
        return pd.DataFrame(columns=columns)
    total = _one_port_totals(ctx, horizon, component).reindex(index).fillna(0.0)
    frame = pd.DataFrame(
        {
            "carrier": static.loc[index, "carrier"],
            "source": static.loc[index, "carrier"],
            "target": static.loc[index, "bus"].map(network.buses.carrier),
            "value": total,
            "origin": origin,
            "port": 0,
            "sign": 0,
        }
    )
    return _group(frame.dropna(subset=["target"]))


def _load_flows(
    ctx: BuildContext,
    network,
    node: str,
    horizon: int,
    rail: float = 0.0,
    overrides: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Demand rows: ``bus carrier -> "<carrier> demand"``.

    ``rail`` is netted out of the generic electricity load because the model
    folds rail traction into it while the legacy tool reports it separately (see
    :func:`_rail_demand`); leaving it in would double-count.
    """
    index = _select(ctx, network, node, "loads", horizon)
    columns = ["carrier", "source", "target", "value", "origin", "port"]
    if len(index) == 0:
        return pd.DataFrame(columns=columns)
    total = _load_totals(ctx, horizon).reindex(index).fillna(0.0)
    loads = network.loads.loc[index]
    frame = pd.DataFrame(
        {
            "carrier": loads["carrier"],
            "source": loads["bus"].map(network.buses.carrier),
            "target": loads["carrier"] + " demand",
            "value": total,
            "origin": "load",
            "port": 0,
            "sign": 0,
        }
    )
    # CO2 "loads" are the process-emission bookkeeping, not energy demand.
    frame = frame[~frame["carrier"].str.contains("emissions")]
    frame = _group(frame.dropna(subset=["source"]))

    is_electricity = frame["carrier"] == "electricity"
    frame.loc[is_electricity, "value"] -= rail
    for carrier, value in (overrides or {}).items():
        hit = frame["carrier"] == carrier
        if hit.any():
            frame.loc[hit, "value"] = value / int(hit.sum())
    return frame


# --------------------------------------------------------------------------
# exogenous rows that are not in the network
# --------------------------------------------------------------------------
def _resource(ctx: BuildContext, names, **kwargs):
    """First of ``names`` that exists under the scenario's resources dir."""
    for name in names:
        if (ctx.resources_dir / name).exists():
            return ctx.read_csv(name, base="resources", **kwargs)
    logger.warning("none of %s found under %s", list(names), ctx.resources_dir)
    return None


def _table_codes(ctx: BuildContext, node: str, index) -> list[str]:
    """Index labels to sum in a per-region table for ``node``.

    Study-wide: every real code present in ``index``.  Group: its members,
    falling back to the group code itself when the table is country-level
    (e.g. a ``BE`` row for members ``BEVLG``/``BEWAL``/``BEBRU``).  Real
    node: ``[node]`` when present.
    """
    locations = ctx.locations_for(node) if hasattr(ctx, "locations_for") else None
    if locations is None:
        if hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node):
            return [c for c in ctx.nodes.real_codes if c in index]
        locations = [node]
    present = [c for c in locations if c in index]
    if present:
        return present
    if node in index:
        return [node]
    return []


def _node_value(ctx: BuildContext, series: pd.Series, node: str) -> float:
    """One node's value out of a per-region series (sum for aggregates)."""
    numeric = pd.to_numeric(series, errors="coerce")
    codes = _table_codes(ctx, node, numeric.index)
    if codes:
        return float(numeric.reindex(codes).sum())
    logger.warning(
        "node %s absent from exogenous table (index: %s)", node, list(numeric.index)[:8]
    )
    return 0.0


def _rail_demand(ctx: BuildContext, node: str, horizon: int) -> float:
    """Rail electricity demand, which the model folds into the generic load."""
    clusters = ctx.config.model.clusters
    table = _resource(
        ctx,
        [
            f"pop_weighted_energy_totals_s_{clusters}_{horizon}.csv",
            f"energy_totals_s_{clusters}_{horizon}.csv",
        ],
        index_col=0,
    )
    if table is None or "total rail" not in table.columns:
        logger.warning("no 'total rail' column for %s: rail demand set to 0", horizon)
        return 0.0
    if "year" in table.columns:
        # The un-weighted table is a historical time series; the legacy code
        # picked `snakemake.params.year`, which no longer exists.  Use the most
        # recent year on record.
        table = table[table["year"] == table["year"].max()]
    return _node_value(ctx, table["total rail"], node)


def _ammonia_demand(ctx: BuildContext, node: str, horizon: int) -> float | None:
    """Industrial ammonia demand, which is not a load in the network."""
    clusters = ctx.config.model.clusters
    name = f"industrial_energy_demand_base_s_{clusters}_{horizon}.csv"
    if not (ctx.resources_dir / name).exists():
        logger.info("%s absent: no ammonia row for %s", name, horizon)
        return None
    table = ctx.read_csv(name, base="resources", index_col=0)
    if table is None or "ammonia" not in table.columns:
        return None
    return _node_value(ctx, table["ammonia"], node)


def _clever_industry(ctx: BuildContext, node: str, horizon: int) -> dict[str, float] | None:
    """Optional CLEVER industry overlay (the legacy ``study == 'suff'`` branch).

    Enabled with ``features: {clever_industry: true}``.  The CSVs live outside
    the model results, so a missing file is a warning, never an error.
    """
    if not ctx.config.raw.get("features", {}).get("clever_industry", False):
        return None
    path = ctx.config.root / "data" / f"clever_Industry_{horizon}.csv"
    if not path.exists():
        logger.warning("features.clever_industry is on but %s is missing -- skipped", path)
        return None
    raw = _cache_get(
        ctx,
        ("csv_abs", str(path), (("index_col", 0),)),
        lambda: pd.read_csv(path, index_col=0),
    )
    table = raw.T
    wanted = {
        "h2_non_energy": "Non-energy consumption of hydrogen for the feedstock production",
        "h2_industry": "Total Final hydrogen consumption in industry",
        "naphtha_industry": "Non-energy consumption of oil for the feedstock production",
        "oil_industry": "Total Final oil consumption in industry",
    }
    out = {}
    for key, row in wanted.items():
        if row not in table.index:
            logger.warning("row %r missing from %s", row, path)
            return None
        out[key] = _node_value(ctx, table.loc[row], node)
    return out


def _extra_rows(
    ctx: BuildContext, node: str, horizon: int, rail: float, clever: dict[str, float] | None
) -> pd.DataFrame:
    """Rows the network does not contain: rail, ammonia, CLEVER overlays."""
    rows = []
    ammonia = _ammonia_demand(ctx, node, horizon)
    if ammonia is not None:
        rows.append(("NH3", "H2", "ammonia for industry", ammonia))

    if clever:
        rows.append(("H2 for non-energy", "hyd", "Non-energy", clever["h2_non_energy"]))
        rows.append(("Oil for industry", "pet", "industry", clever["oil_industry"]))

    rows.append(("Rail Network", "Electricity grid", "Rail Network", rail))
    frame = pd.DataFrame(rows, columns=["carrier", "source", "target", "value"])
    frame["origin"] = "extra"
    frame["port"] = 0
    frame["sign"] = 0
    return frame


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def _aggregate_carriers(flows: pd.DataFrame) -> pd.DataFrame:
    flows = flows.copy()
    for side, needle, replacement in _AGGREGATION_RULES:
        for column in ("source", "target") if side == "both" else (side,):
            hit = flows[column].str.contains(needle, regex=False, na=False)
            flows.loc[hit, column] = replacement
    for column in ("source", "target"):
        hit = flows[column].str.contains("heat", na=False) & ~flows[column].str.contains(
            "demand", na=False
        )
        flows.loc[hit, column] = "heat"
    return flows


def _orient(flows: pd.DataFrame) -> pd.DataFrame:
    """Give every *link* row a positive value and a source -> target direction.

    A negative port flow means the link *feeds* that bus, so the row already
    reads bus0 -> port.  A positive one means it *draws* from the bus, so the
    two ends swap.  ``sign`` is kept because it is part of the entry key: the
    original concatenated the two groups (all fed ports first, then all drawn
    ports), and the ``_2``/``_3`` suffixes in the code table follow from that.

    Only link rows are oriented; generators, stores and loads keep the
    direction they were built with, as in the original.
    """
    flows = flows.copy()
    flows["sign"] = (flows["value"] > 0).astype(int)
    swap = flows["sign"] == 1
    source = flows["source"].copy()
    flows.loc[swap, "source"] = flows.loc[swap, "target"]
    flows.loc[swap, "target"] = source[swap]
    flows["value"] = flows["value"].abs()
    return flows


def _drop_structural(flows: pd.DataFrame) -> pd.DataFrame:
    """Rows that can never be an energy flow, dropped before ranking."""
    source, target = flows["source"], flows["target"]
    keep = (
        (source != target)
        & ~source.str.contains(_CO2, na=False)
        & ~target.str.contains(_CO2, na=False)
        & ~source.str.contains("emissions", na=False)
        & ~source.isin(_DUPLICATE_DEMAND_SOURCES)
    )
    return flows[keep]


def _rank_entries(flows: pd.DataFrame) -> pd.Series:
    """Attach the deterministic ``entry`` name -- see the module docstring."""
    order = flows.copy()
    order["origin_rank"] = order["origin"].map(_ORIGIN_ORDER).fillna(len(_ORIGIN_ORDER))
    order = order.sort_values(
        ["carrier", "origin_rank", "sign", "port", "source", "target"], kind="stable"
    )
    rank = order.groupby("carrier", sort=False).cumcount() + 1
    entry = order["carrier"].where(rank == 1, order["carrier"] + "_" + rank.astype(str))
    return entry.reindex(flows.index)


def _flows_for_horizon(ctx: BuildContext, node: str, horizon: int) -> pd.Series:
    """``entry -> TWh`` for one node and one planning horizon."""
    network = ctx.networks[horizon]
    totals = _link_port_totals(ctx, horizon)

    links = _select(ctx, network, node, "links", horizon)
    flows = _link_flows(network, links, totals=totals)
    flows = _heat_pump_rows(network, links, flows, totals=totals)
    flows = _orient(flows)

    # DAC has no single output bus: the legacy code relabelled every DAC row so
    # that the CO2 ports survive the CO2 filter below.  Kept for compatibility
    # with the `predac*` codes.
    flows.loc[flows["carrier"] == "DAC", "target"] = "DAC"

    parts = [flows]
    for component, origin in (
        ("generators", "generator"),
        ("storage_units", "storage_unit"),
        ("stores", "store"),
    ):
        parts.append(_one_port_flows(ctx, network, node, horizon, component, origin))

    rail = _rail_demand(ctx, node, horizon)
    clever = _clever_industry(ctx, node, horizon)
    overrides = (
        {
            "H2 for industry": clever["h2_industry"],
            "naphtha for industry": clever["naphtha_industry"],
        }
        if clever
        else None
    )
    parts.append(_load_flows(ctx, network, node, horizon, rail=rail, overrides=overrides))
    parts.append(_extra_rows(ctx, node, horizon, rail, clever))

    # Empty frames are dropped rather than concatenated: an all-NA frame makes
    # pandas widen the dtypes of the result (FutureWarning in 2.x).
    everything = pd.concat([part for part in parts if len(part)], ignore_index=True)
    # The two fossil commodities are renamed so that the generator row does not
    # collapse onto `source == target`.
    fossil = {"gas": "fossil gas", "oil": "fossil oil"}
    generator = everything["origin"] == "generator"
    for column in ("carrier", "source"):
        everything.loc[generator, column] = everything.loc[generator, column].replace(fossil)

    everything = _aggregate_carriers(everything)
    everything = _drop_structural(everything)
    everything["entry"] = _rank_entries(everything)
    flat = everything.set_index("entry")["value"].astype(float)
    if flat.index.has_duplicates:
        clashing = sorted(set(flat.index[flat.index.duplicated()]))
        logger.warning("entry name collision, summing: %s", clashing)
        flat = flat.groupby(level=0).sum()
    return flat


def _assemble(
    ctx: BuildContext, values: dict[str, pd.Series], table: pd.DataFrame
) -> pd.DataFrame:
    """Join the per-horizon series onto the carrier->code table."""
    threshold = ctx.config.model.flow_threshold
    out = table[["entry", "label", "unit", "code"]].copy()
    for column, series in values.items():
        mapped = out["entry"].map(series).astype(float).fillna(0.0)
        # Drop by *magnitude*: the legacy `value >= 0.1` deleted every negative
        # number, which silently removed net-negative rows.
        out[column] = mapped.where(mapped.abs() >= threshold, 0.0)

    known = set(table["entry"])
    for column, series in values.items():
        unknown = sorted(set(series.index) - known)
        if unknown:
            logger.debug("%s: %d flow(s) with no code: %s", column, len(unknown), unknown)

    columns = list(values)
    aggregation = {"label": "first", "unit": "first", **{c: "sum" for c in columns}}
    # Several entries legitimately share one code (e.g. `prohydclamm`); the
    # legacy writer summed them with a groupby on the code column.
    grouped = out.groupby("code", as_index=False, sort=False).agg(aggregation)
    return grouped[["code", "label", "unit", *columns]]


def energy_flows(ctx: BuildContext, node: str) -> pd.DataFrame:
    """Long energy-flow table for one node.

    Columns: ``code, label, unit`` plus one column per ``ctx.year_columns``,
    in TWh.  Every code of ``data/carrier_flows_energy.csv`` is present, at
    ``0.0`` when the carrier does not exist in the model.

    Snapshot-weighted port totals are computed once per horizon and shared
    across nodes; the assembled table is cached per node so ``demand_table``
    and ``indicators.for_node`` do not re-extract.
    """
    cache_key = ("energy_flows", str(node))
    if cache_key in ctx._files:
        return ctx._files[cache_key].copy()

    values: dict[str, pd.Series] = {}
    for column in ctx.year_columns:
        horizon = int(column)
        if horizon not in ctx.networks:
            logger.warning("no network for %s: column filled with zeros", column)
            values[column] = pd.Series(dtype=float)
            continue
        logger.info("energy flows for node %s, horizon %s", node, horizon)
        values[column] = _flows_for_horizon(ctx, node, horizon)
    result = _assemble(ctx, values, ctx.taxonomy.carrier_flows_energy)
    ctx._files[cache_key] = result
    return result.copy()
