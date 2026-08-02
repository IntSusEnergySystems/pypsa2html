"""Carbon flows per node and horizon -- the port of legacy ``prepare_emissions``.

The original was 1000 lines of copy-paste: 66 near-identical blocks, each one
an ``if country == 'EU': ... else: ...`` pair around a
``n.links_t.pN.filter(like=...)`` reduction, eight of which were guarded with
``if "p3" in n.links_t`` and ten of which were not.  Every block is one row of
:data:`CARBON_FLOWS` here, and the guard is applied once, in
:func:`~pypsa2html.extract.flows._port_total`.

The deterministic entry key
---------------------------
As on the energy side (see :mod:`pypsa2html.extract.flows`) the legacy code
named its rows by *order of survival*: ``prepare_emissions`` dropped every row
below 0.1 Mt, ``write_to_excel`` merged the horizons and only then appended
``_2``, ``_3``, ... in whatever order the merge had produced.  A row that
existed in 2040 but not in 2030 therefore renamed its siblings.

Here the key is the position of the row *in the table below*: the k-th
occurrence of a ``label`` becomes ``label`` (k = 1) or ``f"{label}_{k}"``,
which is the ``entry`` column of ``data/carrier_flows_carbon.csv``.  The table
order is the order of the original ``collection.append(...)`` calls, so the
resulting names are the ones the code table already knows -- but they no
longer depend on any value.

Deliberate differences from the négaWatt original
-------------------------------------------------
* ``naphtha for industry`` reads port 2 of the *naphtha* links.  The négaWatt
  version read port 1 of the *waste CHP* links (a copy-paste slip that reported
  waste-CHP electricity as an oil emission); the pypsa-wal fork had already
  fixed it and that is the version kept here.
* ``atm`` is spelled ``co2 atmosphere`` everywhere.  In the négaWatt file three
  rows (waste CHP, waste CHP CC, HVC to air) used the short spelling, which
  excluded them from the ``net co2 emissions`` balance; pypsa-wal fixed that
  too.
* ``fossil gas`` selects generators whose carrier *is* ``gas``.  The original
  matched the substring ``gas`` in the generator *name*, which also caught
  ``<node> biogas``.
* CO2 intensities come from :meth:`~pypsa2html.context.BuildContext.cost`;
  ``float(options.loc[(tech, param)])`` raises on pandas >= 2.2 because the
  legacy read a wide table with ``index_col=[0, 1]``.

Quirks kept on purpose
----------------------
Six rows use ``match="prefix"`` because the original matched a substring of
the link *name* and therefore swept up the CC variant of the technology as
well: ``coal`` also counts ``coal for industry``, ``SMR`` also counts
``SMR CC``, and ``biogas to gas``, ``urban central gas CHP``, ``waste CHP``
and ``urban central solid biomass CHP`` also count their ``CC`` siblings even
though those have rows of their own.  For ``biogas to gas`` this mixes a
port that feeds the atmosphere with one that feeds the CO2 store, so the row
turns negative once carbon capture dominates.  The behaviour is reproduced
rather than corrected so that the numbers still match the published sheets;
switching any of these to ``match="exact"`` is a one-word change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from ..context import BuildContext
from .flows import (
    _cache_get,
    _link_port_totals,
    _node_value,
    _one_port_totals,
    _resource,
    _select,
)

logger = logging.getLogger(__name__)

#: Bus names used as the graph vocabulary of the carbon Sankey.  ``co2
#: atmosphere`` and ``co2 stored`` are load-bearing: the sequestration and net
#: emission rows are derived by summing over them.
ATMOSPHERE = "co2 atmosphere"
STORED = "co2 stored"

#: Fallbacks when the cost table has no CO2 intensity for a fuel, in t/MWh.
#: Taken from the technology-data defaults both models ship with; a mismatch is
#: logged rather than silently turning an emission into zero.
_DEFAULT_INTENSITY = {
    ("gas", "CO2 intensity"): 0.198,
    ("oil", "CO2 intensity"): 0.2571,
    ("coal", "CO2 intensity"): 0.3361,
    ("lignite", "CO2 intensity"): 0.4069,
    ("solid biomass", "CO2 intensity"): 0.3667,
    ("BtL", "CO2 stored"): 0.251,
}


@dataclass(frozen=True)
class CarbonFlow:
    """One row of the carbon balance.

    ``carriers`` are matched against the *component carrier*, not against the
    component name: ``filter(like="coal")`` used to catch ``coal for industry``
    as well, and ``filter(like="BE")`` caught ``BEWAL``.  Where the original
    deliberately relied on that (``SMR`` meaning "SMR and SMR CC") the match
    mode is ``prefix``.
    """

    label: str
    source: str
    target: str
    carriers: tuple[str, ...]
    port: int = 0
    component: str = "links"
    sign: float = -1.0
    #: ``(technology, parameter)`` multiplied in, for flows measured in MWh.
    intensity: tuple[str, str] | None = None
    match: str = "exact"
    #: Reuse the value of the preceding row instead of recomputing it -- the
    #: original appended the same number twice, once per direction.
    same_as_previous: bool = False


#: The carbon balance, in the order the legacy ``collection.append`` calls ran.
#: **The order defines the entry names** -- do not reorder without updating
#: ``data/carrier_flows_carbon.csv``.
CARBON_FLOWS: tuple[CarbonFlow, ...] = (
    CarbonFlow("DAC", ATMOSPHERE, STORED, ("DAC",), 3),
    CarbonFlow("process emissions", "process emissions", ATMOSPHERE, ("process emissions",), 1),
    CarbonFlow("process emissions CC", "process emissions", STORED, ("process emissions CC",), 2),
    # Fossil power plants.  Each technology appears twice: once as a link with
    # a CO2 port, once as a generator burning fuel implicitly.  A given plant
    # is only ever one of the two, so the rows never overlap.
    CarbonFlow("OCGT", "gas", ATMOSPHERE, ("OCGT",), 2, match="prefix"),
    CarbonFlow("OCGT", "gas", ATMOSPHERE, ("OCGT",), component="generators", sign=1.0,
               intensity=("gas", "CO2 intensity"), match="prefix"),
    CarbonFlow("CCGT", "gas", ATMOSPHERE, ("CCGT",), 2, match="prefix"),
    CarbonFlow("CCGT", "gas", ATMOSPHERE, ("CCGT",), component="generators", sign=1.0,
               intensity=("gas", "CO2 intensity"), match="prefix"),
    CarbonFlow("lignite", "coal", ATMOSPHERE, ("lignite",), 2, match="prefix"),
    CarbonFlow("lignite", "coal", ATMOSPHERE, ("lignite",), component="generators", sign=1.0,
               intensity=("lignite", "CO2 intensity"), match="prefix"),
    CarbonFlow("coal", "coal", ATMOSPHERE, ("coal",), 2, match="prefix"),
    CarbonFlow("coal", "coal", ATMOSPHERE, ("coal",), component="generators", sign=1.0,
               intensity=("coal", "CO2 intensity"), match="prefix"),
    CarbonFlow("Sabatier", STORED, "gas", ("Sabatier",), 2, sign=1.0, match="prefix"),
    CarbonFlow("SMR", "gas", ATMOSPHERE, ("SMR",), 2, match="prefix"),
    CarbonFlow("SMR CC", "gas", ATMOSPHERE, ("SMR CC",), 2, match="prefix"),
    CarbonFlow("SMR CC", "gas", STORED, ("SMR CC",), 3, match="prefix"),
    CarbonFlow("rural gas boiler", "gas", ATMOSPHERE, ("rural gas boiler",), 2),
    CarbonFlow("urban decentral gas boiler", "gas", ATMOSPHERE, ("urban decentral gas boiler",), 2),
    CarbonFlow("urban central gas boiler", "gas", ATMOSPHERE, ("urban central gas boiler",), 2),
    CarbonFlow("rural oil boiler", "oil", ATMOSPHERE, ("rural oil boiler",), 2),
    CarbonFlow("urban decentral oil boiler", "oil", ATMOSPHERE, ("urban decentral oil boiler",), 2),
    CarbonFlow("urban central oil boiler", "oil", ATMOSPHERE, ("urban central oil boiler",), 2),
    # Biogas: the CO2 comes out of the atmosphere and goes back into the gas
    # grid, so the same number is booked twice, in opposite directions.
    CarbonFlow("biogas to gas", ATMOSPHERE, "biogas", ("biogas to gas",), 2, sign=1.0,
               match="prefix"),
    CarbonFlow("biogas to gas", "biogas", "gas", ("biogas to gas",), 2, sign=1.0, match="prefix",
               same_as_previous=True),
    CarbonFlow("biogas to gas CC", "biogas", STORED, ("biogas to gas CC",), 2),
    CarbonFlow("biogas to gas CC2", ATMOSPHERE, "biogas", ("biogas to gas CC",), 3, sign=1.0),
    CarbonFlow("biogas to gas CC3", "biogas", "gas", ("biogas to gas CC",), 1,
               intensity=("gas", "CO2 intensity")),
    CarbonFlow("solid biomass for industry", "solid biomass", ATMOSPHERE,
               ("solid biomass for industry",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity")),
    CarbonFlow("solid biomass for industry", ATMOSPHERE, "solid biomass",
               ("solid biomass for industry",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity"), same_as_previous=True),
    CarbonFlow("solid biomass for industry CC", ATMOSPHERE, "BECCS",
               ("solid biomass for industry CC",), 2, sign=1.0),
    CarbonFlow("solid biomass for industry CC", "BECCS", STORED,
               ("solid biomass for industry CC",), 3),
    CarbonFlow("methanolisation", STORED, "methanol", ("methanolisation",), 3, sign=1.0,
               match="prefix"),
    CarbonFlow("gas for industry", "gas", ATMOSPHERE, ("gas for industry",), 2),
    CarbonFlow("gas for industry CC1", "gas", ATMOSPHERE, ("gas for industry CC",), 2),
    CarbonFlow("gas for industry CC2", "gas", STORED, ("gas for industry CC",), 3),
    # BioSNG: the model calls the link "<node> solid biomass solid biomass to
    # gas", the carrier is "BioSNG".  The legacy filter matched the name.
    CarbonFlow("solid biomass solid biomass to gas", "solid biomass", "gas", ("BioSNG",), 1,
               intensity=("gas", "CO2 intensity")),
    CarbonFlow("solid biomass solid biomass to gas1", ATMOSPHERE, "solid biomass", ("BioSNG",), 1,
               intensity=("gas", "CO2 intensity"), same_as_previous=True),
    CarbonFlow("solid biomass solid biomass to gas CC1", "solid biomass", "gas", ("BioSNG CC",), 1,
               intensity=("gas", "CO2 intensity")),
    CarbonFlow("solid biomass solid biomass to gas CC2", ATMOSPHERE, "solid biomass",
               ("BioSNG CC",), 1, intensity=("gas", "CO2 intensity"), same_as_previous=True),
    CarbonFlow("solid biomass solid biomass to gas CC3", "solid biomass", STORED, ("BioSNG CC",), 2),
    CarbonFlow("solid biomass solid biomass to gas CC4", ATMOSPHERE, "solid biomass",
               ("BioSNG CC",), 2, same_as_previous=True),
    CarbonFlow("urban decentral biomass boiler", "solid biomass", ATMOSPHERE,
               ("urban decentral biomass boiler",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity")),
    CarbonFlow("urban decentral biomass boiler", ATMOSPHERE, "solid biomass",
               ("urban decentral biomass boiler",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity"), same_as_previous=True),
    CarbonFlow("rural biomass boiler", "solid biomass", ATMOSPHERE, ("rural biomass boiler",), 0,
               sign=1.0, intensity=("solid biomass", "CO2 intensity")),
    CarbonFlow("rural biomass boiler", ATMOSPHERE, "solid biomass", ("rural biomass boiler",), 0,
               sign=1.0, intensity=("solid biomass", "CO2 intensity"), same_as_previous=True),
    CarbonFlow("solid biomass biomass to liquid", "solid biomass", "oil", ("biomass to liquid",), 1,
               intensity=("BtL", "CO2 stored")),
    CarbonFlow("solid biomass biomass to liquid", ATMOSPHERE, "solid biomass",
               ("biomass to liquid",), 1, intensity=("BtL", "CO2 stored"), same_as_previous=True),
    CarbonFlow("solid biomass biomass to liquid CC", "solid biomass", STORED,
               ("biomass to liquid CC",), 3),
    CarbonFlow("solid biomass biomass to liquid CC", ATMOSPHERE, "solid biomass",
               ("biomass to liquid CC",), 3, same_as_previous=True),
    CarbonFlow("Fischer-Tropsch", STORED, "oil", ("Fischer-Tropsch",), 2, sign=1.0),
    CarbonFlow("urban central gas CHP", "gas", ATMOSPHERE, ("urban central gas CHP",), 3,
               match="prefix"),
    CarbonFlow("urban central gas CHP CC1", "gas", ATMOSPHERE, ("urban central gas CHP CC",), 3),
    CarbonFlow("urban central gas CHP CC2", "gas", STORED, ("urban central gas CHP CC",), 4),
    CarbonFlow("urban central solid biomass CHP", "solid biomass", ATMOSPHERE,
               ("urban central solid biomass CHP",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity"), match="prefix"),
    CarbonFlow("urban central solid biomass CHP", ATMOSPHERE, "solid biomass",
               ("urban central solid biomass CHP",), 0, sign=1.0,
               intensity=("solid biomass", "CO2 intensity"), match="prefix",
               same_as_previous=True),
    CarbonFlow("urban central solid biomass CHP CC", ATMOSPHERE, "BECCS",
               ("urban central solid biomass CHP CC",), 3, sign=1.0),
    CarbonFlow("urban central solid biomass CHP CC", "BECCS", STORED,
               ("urban central solid biomass CHP CC",), 4),
    CarbonFlow("naphtha for industry", "oil", "process emissions", ("naphtha for industry",), 2),
    CarbonFlow("kerosene for aviation", "oil", ATMOSPHERE, ("kerosene for aviation",), 2),
    CarbonFlow("oil refining emissions", "oil", ATMOSPHERE, ("oil refining",), 2),
    CarbonFlow("agriculture machinery oil emissions", "oil", ATMOSPHERE,
               ("agriculture machinery oil",), 2),
    CarbonFlow("land transport oil emissions", "oil", ATMOSPHERE, ("land transport oil",), 2),
    CarbonFlow("shipping oil emissions", "oil", ATMOSPHERE, ("shipping oil",), 2),
    CarbonFlow("shipping methanol emissions", "methanol", ATMOSPHERE, ("shipping methanol",), 2),
    CarbonFlow("Electrobiofuels", "solid biomass", "oil", ("electrobiofuels",), 3, sign=1.0),
    CarbonFlow("Electrobiofuels", ATMOSPHERE, "solid biomass", ("electrobiofuels",), 3, sign=1.0,
               same_as_previous=True),
    CarbonFlow("Waste CHP emissions", "Waste CHP", ATMOSPHERE, ("waste CHP",), 3, match="prefix"),
    CarbonFlow("Waste CHP CC", "Waste CHP", STORED, ("waste CHP CC",), 4),
    CarbonFlow("Waste CHP CC1", "Waste CHP", ATMOSPHERE, ("waste CHP CC",), 3),
    CarbonFlow("HVC to air", "oil", ATMOSPHERE, ("HVC to air",), 1),
    CarbonFlow("fossil gas", "fossil gas", "gas", ("gas",), component="generators", sign=1.0,
               intensity=("gas", "CO2 intensity")),
    CarbonFlow("fossil oil", "fossil oil", "oil", ("oil refining",), 1,
               intensity=("oil", "CO2 intensity")),
)


# --------------------------------------------------------------------------
def _carrier_mask(static: pd.DataFrame, flow: CarbonFlow) -> pd.Series:
    carriers = static["carrier"].astype(str)
    if flow.match == "prefix":
        hit = pd.Series(False, index=static.index)
        for carrier in flow.carriers:
            hit |= carriers.str.startswith(carrier)
        return hit
    return carriers.isin(flow.carriers)


class _Reductions:
    """Per-horizon annual totals, computed once for all 66 rows.

    Link and generator matmuls are shared across nodes via the horizon caches
    in :mod:`pypsa2html.extract.flows`; each node only reindexes the result.
    """

    def __init__(self, ctx: BuildContext, node: str, horizon: int):
        self.network = ctx.networks[horizon]
        self.selection = {
            component: _select(ctx, self.network, node, component, horizon)
            for component in ("links", "generators")
        }
        # Full-network totals, sliced to this node's selection.
        full_links = _link_port_totals(ctx, horizon)
        self.link_totals = {
            port: series.reindex(self.selection["links"]).fillna(0.0)
            for port, series in full_links.items()
        }
        self.generator_totals = (
            _one_port_totals(ctx, horizon, "generators")
            .reindex(self.selection["generators"])
            .fillna(0.0)
        )

    def total(self, flow: CarbonFlow) -> float:
        static = getattr(self.network, flow.component).loc[self.selection[flow.component]]
        selected = static.index[_carrier_mask(static, flow)]
        if len(selected) == 0:
            return 0.0
        if flow.component == "links":
            # Already scaled by 1e6: tCO2 -> Mt for a CO2 port, MWh -> TWh for
            # an energy port, which is the factor the intensity below needs.
            series = self.link_totals.get(flow.port)
            if series is None:  # port absent from this network
                return 0.0
        else:
            series = self.generator_totals
        return float(series.reindex(selected).fillna(0.0).sum())


def _flow_value(ctx: BuildContext, reductions: _Reductions, flow: CarbonFlow) -> float:
    """One row of the carbon balance, in MtCO2."""
    value = flow.sign * reductions.total(flow)
    if flow.intensity is not None:
        value *= ctx.cost(*flow.intensity, default=_DEFAULT_INTENSITY.get(flow.intensity, 0.0))
    return value


def _entry_names(flows: tuple[CarbonFlow, ...]) -> list[str]:
    """``label``, ``label_2``, ... by position in the table."""
    seen: dict[str, int] = {}
    names = []
    for flow in flows:
        seen[flow.label] = seen.get(flow.label, 0) + 1
        rank = seen[flow.label]
        names.append(flow.label if rank == 1 else f"{flow.label}_{rank}")
    return names


# --------------------------------------------------------------------------
# rows that come from outside the network
# --------------------------------------------------------------------------
def _lulucf(ctx: BuildContext, node: str, horizon: int) -> float | None:
    """Land-use sink, from the exogenous CO2 totals.

    Enabled with ``features: {lulucf: true}`` -- in pypsa-wal the whole block
    sat behind a ``suff_demand`` config switch, in négaWatt it was
    unconditional.  Positive (source) values are clipped away: only the sink
    part is reported, negated so that it reads as a removal.
    """
    if not ctx.config.raw.get("features", {}).get("lulucf", False):
        return None
    clusters = ctx.config.model.clusters
    table = _resource(
        ctx,
        [
            f"co2_totals_s_{clusters}_{horizon}.csv",
            f"co2_totals_{clusters}_{horizon}.csv",
        ],
        index_col=0,
    )
    if table is None or "LULUCF" not in table.columns:
        logger.warning("features.lulucf is on but no LULUCF column was found -- skipped")
        return None
    sink = pd.to_numeric(table["LULUCF"], errors="coerce").clip(upper=0.0)
    return -_node_value(ctx, sink, node)


def _agriculture_ghg(ctx: BuildContext, node: str, horizon: int) -> float | None:
    """Non-energy CH4 + N2O from agriculture, from the CLEVER AFOLU overlay.

    Enabled with ``features: {agriculture_ghg: true}``.  The file lives in the
    model repository rather than in its results, so it is optional: a missing
    file is a warning.
    """
    if not ctx.config.raw.get("features", {}).get("agriculture_ghg", False):
        return None
    path = ctx.config.root / "data" / f"clever_AFOLUB_{horizon}.csv"
    if not path.exists():
        logger.warning("features.agriculture_ghg is on but %s is missing -- skipped", path)
        return None
    table = _cache_get(
        ctx,
        ("csv_abs", str(path), (("index_col", 0),)),
        lambda: pd.read_csv(path, index_col=0),
    )
    columns = ["Total CH4 emissions from agriculture", "Total N2O emissions from agriculture"]
    missing = [c for c in columns if c not in table.columns]
    if missing:
        logger.warning("%s has no column(s) %s -- skipped", path, missing)
        return None
    return _node_value(ctx, table[columns].sum(axis=1), node)


# --------------------------------------------------------------------------
def _balance_for_horizon(ctx: BuildContext, node: str, horizon: int) -> pd.DataFrame:
    """The full carbon balance for one node and horizon, in MtCO2."""
    reductions = _Reductions(ctx, node, horizon)

    rows = []
    previous = 0.0
    for flow, entry in zip(CARBON_FLOWS, _entry_names(CARBON_FLOWS)):
        value = previous if flow.same_as_previous else _flow_value(ctx, reductions, flow)
        previous = value
        rows.append((entry, flow.label, flow.source, flow.target, value))
    balance = pd.DataFrame(rows, columns=["entry", "label", "source", "target", "value"])

    def _net(bus: str) -> float:
        into = balance.loc[balance["target"] == bus, "value"].sum()
        out = balance.loc[balance["source"] == bus, "value"].sum()
        return float(into - out)

    # Order matters: sequestration is a balance over the rows above it, the net
    # emission is a balance over everything including the exogenous rows.
    extras: list[tuple] = [
        ("co2 sequestration", "co2 sequestration", STORED, "co2 sequestration", _net(STORED))
    ]

    lulucf = _lulucf(ctx, node, horizon)
    if lulucf is not None:
        extras.append(("LULUCF", "LULUCF", ATMOSPHERE, "LULUCF", lulucf))
    agriculture = _agriculture_ghg(ctx, node, horizon)
    if agriculture is not None:
        extras.append(
            (
                "Non-energy GHG Agriculture",
                "Non-energy GHG Agriculture",
                "GHG Agriculture",
                ATMOSPHERE,
                agriculture,
            )
        )

    balance = pd.concat(
        [balance, pd.DataFrame(extras, columns=balance.columns)],
        ignore_index=True,
    )
    balance = pd.concat(
        [
            balance,
            pd.DataFrame(
                [
                    (
                        "net co2 emissions",
                        "net co2 emissions",
                        ATMOSPHERE,
                        "net co2 emissions",
                        _net(ATMOSPHERE),
                    )
                ],
                columns=balance.columns,
            ),
        ],
        ignore_index=True,
    )
    return balance


def carbon_flows(ctx: BuildContext, node: str) -> pd.DataFrame:
    """Long carbon-flow table for one node.

    Columns: ``code, label, unit`` plus one column per ``ctx.year_columns``,
    in MtCO2.  Every code of ``data/carrier_flows_carbon.csv`` is present, at
    ``0.0`` when the process does not exist in the model.

    Snapshot-weighted totals are shared with energy extraction via the
    horizon caches; the assembled table is cached per node.
    """
    cache_key = ("carbon_flows", str(node))
    if cache_key in ctx._files:
        return ctx._files[cache_key].copy()

    values: dict[str, pd.Series] = {}
    for column in ctx.year_columns:
        horizon = int(column)
        if horizon not in ctx.networks:
            logger.warning("no network for %s: column filled with zeros", column)
            values[column] = pd.Series(dtype=float)
            continue
        logger.info("carbon flows for node %s, horizon %s", node, horizon)
        balance = _balance_for_horizon(ctx, node, horizon)
        values[column] = balance.set_index("entry")["value"].astype(float)

    table = ctx.taxonomy.carrier_flows_carbon
    threshold = ctx.config.model.flow_threshold
    out = table[["entry", "label", "unit", "code"]].copy()
    for column, series in values.items():
        mapped = out["entry"].map(series).astype(float).fillna(0.0)
        # By magnitude: a net-negative CO2 row (net-zero systems have several)
        # is a result, not noise.  ``value >= 0.1`` deleted all of them.
        out[column] = mapped.where(mapped.abs() >= threshold, 0.0)

    known = set(table["entry"])
    for column, series in values.items():
        unknown = sorted(set(series.index) - known)
        if unknown:
            logger.debug(
                "%s: %d carbon row(s) with no code: %s", column, len(unknown), unknown
            )

    columns = list(values)
    aggregation = {"label": "first", "unit": "first", **{c: "sum" for c in columns}}
    grouped = out.groupby("code", as_index=False, sort=False).agg(aggregation)
    result = grouped[["code", "label", "unit", *columns]]
    ctx._files[cache_key] = result
    return result.copy()
