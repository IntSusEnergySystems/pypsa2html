"""The indicator flow algebra: from a long flow table to every derived indicator.

This is the port of the legacy graph-construction and results steps.  All HTML
emission, all file writing and all ``snakemake`` access are gone; a builder
asks for :func:`for_node` and gets an :class:`Indicators` object whose members
are computed on first use.

What the algebra does
---------------------
The extraction layer produces one *long* table per node (``code``, ``label``,
``unit`` and one column per entry in ``ctx.year_columns``).  Those codes are
model outputs -- "electricity from CCGT", "Fischer-Tropsch output" -- and they
only become an energy-system *graph* once they are attached to the edges
declared in ``data/processes_*.csv``.  That is step one.  Step two closes the
graph: several flows are not model outputs at all but balances that have to be
derived (fossil gas is whatever the gas grid consumed minus the renewable gas
injected; imports are whatever demand exceeded domestic production; nuclear
thermal losses follow from the reactor efficiency in the cost table).

Only then do the indicators fall out: final consumption by carrier and by
sector, coverage and renewable-share ratios, and the greenhouse-gas balance by
emitting sector and by emission source.

Differences from the legacy implementation
------------------------------------------
Beyond the mechanical changes required by ``docs/INTERNALS.md`` (no
``snakemake``, no module globals, no ``'EU'`` literal, no ``groupby(axis=1)``,
no mutable default arguments, no year literals -- horizons and their weights
come from ``ctx``), the following confirmed defects are fixed here.  Each is
marked ``FIX n`` at the point where it is repaired.

FIX 1 -- Inside ``for en_code in ['amm','met']`` the loss flow was written to
    the hardcoded target ``('met_fe', 'per', '')`` instead of
    ``(en_code + '_fe', 'per', '')``.  The ammonia synthesis losses were
    therefore never created and the methanol ones were written twice (the
    second write being identical, so the symptom was silent).  The aggregate
    branch was the same statement without the loop and is now looped as well,
    so ammonia behaves like methanol there too.

FIX 2 -- ``ren_bm = ['enc_pe']`` after ``bm_columns = ['enc_pe']`` (and the
    same pattern for ``pac``, ``amm`` and ``met``): the numerator list equalled
    the denominator list, so four of the nine renewable-share ratios were 100%
    by construction, or NaN when the carrier was absent.  The denominator is
    now *every* source that actually feeds the carrier in the flow table,
    discovered rather than restated -- see :data:`RENEWABLE_SHARES` and
    :class:`_CarrierMix`.

FIX 3 -- the legacy ``cumul`` helper mutated ``input_df.index`` in place, so
    the caller's frame silently changed dtype.
    :meth:`Indicators.ghg_sector_cum` never touches its input.

FIX 4 -- cumulative emissions were weighted with a literal ``*= 10`` (the
    pypsa-wal fork bolted a ``*= 5`` on top for its 2025 base year) and left
    the *first* horizon unweighted, so a four-horizon pathway counted 31
    years, not 40.  ``ctx.horizon_weights()`` now supplies one weight per
    horizon, whatever the spacing.

FIX 5 -- ~15 places addressed rows by the literals
    ``'2020' / '2030' / '2040' / '2050'``, which made a 2035 horizon
    structurally impossible and silently left the base year as NaN in the CO2
    frame.  Every such block is now a vectorised expression over the whole
    index.

FIX 6 -- duplicate graph edges.  ``data/processes_energy.csv`` declares 130
    rows that share a ``(Source, Target, Type)`` triple with another row (13
    distinct PV/wind/hydro technologies all feed ``spv_pe -> elc_se`` etc.).
    The legacy frame therefore had duplicate columns, which is why ~40 reads in
    the original are wrapped in ``.squeeze()`` -- and why writing to such a key
    overwrote *every* duplicate with the same value.  Duplicates are summed
    once, here, so the frame has unique columns and assignment is well defined.

Two model-specific corrections from the pypsa-wal fork are folded in
unconditionally, because both reduce to a no-op when the corresponding process
is absent from the input: the electricity distribution losses already contained
in the residential demand (``elc_se -> per`` type ``dis``) and the
biomass-to-liquid term of the fossil-oil node (``bm_ghg -> oil_ghg`` type
``bm``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from copy import copy
from dataclasses import dataclass
from functools import cached_property

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Topology constants
#
# These lists describe the *shape* of the energy-system graph rather than a per
# technology value, so unlike colours or emission factors they are not a CSV
# lookup.  They are stated once, here, instead of being spelled out inline in
# twenty ``for en_code in [...]`` loops as in the original.
# ---------------------------------------------------------------------------

#: Carriers delivered from a secondary-energy network straight to final use.
SE_TO_FE_CARRIERS: tuple[str, ...] = ("vap", "elc", "gaz", "hyd", "bev")

#: Carriers delivered from a primary energy straight to final use.
PE_TO_FE_CARRIERS: tuple[str, ...] = ("pac", "enc", "cms")

#: Renewable injections into the gas grid, subtracted to obtain fossil gas.
GAS_GRID_RENEWABLE: tuple[tuple[str, str, str], ...] = (
    ("bgl_pe", "gaz_se", ""),
    ("bgl_pe", "gaz_se", "cc"),
    ("enc_pe", "gaz_se", ""),
    ("enc_pe", "gaz_se", "cc"),
    ("hyd_se", "gaz_se", ""),
)

#: Primary energies whose supply is by definition domestic.
DOMESTIC_CARRIERS: tuple[str, ...] = (
    "hdr",
    "eon",
    "eof",
    "spv",
    "pac",
    "bgl",
    "win",
    "wst",
)

#: Primary energies whose supply is by definition imported.
IMPORTED_CARRIERS: tuple[str, ...] = ("cms", "ura")

#: Secondary carriers balanced against the rest of the system by trade.
TRADED_SE_CARRIERS: tuple[str, ...] = ("elc", "hyd")

#: Taxonomy types that are transformation nodes.  After graph closure, inflow
#: must equal outflow.  Sources (``prod``, ``imp``), sinks (demand sectors,
#: ``per``, ``exp``) and GHG nodes are exempt — they are meant to be open.
BALANCE_NODE_TYPES: tuple[str, ...] = (
    "PRIMARY_ENERGIES",
    "SECONDARY_ENERGIES",
    "FINAL_ENERGIES",
)

#: Final carriers balanced against the rest of the system by trade.
TRADED_FE_CARRIERS: tuple[str, ...] = ("amm", "met")

#: Nodes whose total inflow is the denominator of a per-carrier coverage ratio.
BALANCE_TARGETS: tuple[str, ...] = (
    "elc_se",
    "cms_pe",
    "met_fe",
    "hyd_se",
    "gaz_pe",
    "amm_fe",
    "enc_pe",
    "vap_se",
    "pet_pe",
)

#: Public columns of :class:`SelfSufficiency.ratio` and the taxonomy codes
#: used when those series are drawn as a chart.
SELF_SUFFICIENCY_KINDS: tuple[tuple[str, str], ...] = (
    ("primary", "ss_pe"),
    ("electricity", "ss_elc"),
)

#: ``features.nuclear_primary`` values. Default ``uranium`` keeps fuel as an
#: import; ``electricity`` books reactor kWh as domestic primary energy.
NUCLEAR_PRIMARY_URANIUM = "uranium"
NUCLEAR_PRIMARY_ELECTRICITY = "electricity"
NUCLEAR_PRIMARY_LABELS = {
    NUCLEAR_PRIMARY_URANIUM: "nuclear as imported uranium",
    NUCLEAR_PRIMARY_ELECTRICITY: "nuclear as domestic electricity",
}

#: GHG nodes that represent a *removal*; their bar is drawn below the axis.
GHG_REMOVALS: tuple[str, ...] = (
    "lufnes_ghg",
    "blg_ghg",
    "dac_ghg",
    "blq_ghg",
    "bmc_ghg",
)

#: Columns of ``biomass_potentials_s_*.csv`` that make up solid biomass.
#: négaWatt read only the first; pypsa-wal added the second when its model
#: gained an unsustainable-biomass carrier.  Summing whatever is present serves
#: both: a column that does not exist contributes nothing.
BIOMASS_POTENTIAL_COLUMNS: tuple[str, ...] = (
    "solid biomass",
    "unsustainable solid biomass",
)

#: Flows reported as imports in the per-node trade summary.
IMPORT_FLOWS: tuple[tuple[str, str, str], ...] = (
    ("imp", "gaz_pe", ""),
    ("imp", "pet_pe", ""),
    ("imp", "elc_se", ""),
    ("imp", "hyd_se", ""),
    ("imp", "enc_pe", ""),
    ("imp", "amm_fe", ""),
    ("imp", "met_fe", ""),
    ("ura_pe", "elc_se", "thm"),
    ("imp", "cms_pe", ""),
)

#: Flows reported as domestic production in the per-node trade summary.
LOCAL_PRODUCTION_FLOWS: tuple[tuple[str, str, str], ...] = tuple(
    ("prod", f"{c}_pe", "")
    for c in ("gaz", "pet", "hdr", "eon", "eof", "enc", "spv", "pac", "cms", "bgl", "win")
)

#: Flows reported as exports in the per-node trade summary.
EXPORT_FLOWS: tuple[tuple[str, str, str], ...] = (
    ("elc_se", "exp", ""),
    ("hyd_se", "exp", ""),
    ("enc_pe", "exp", ""),
    ("met_fe", "exp", ""),
    ("amm_fe", "exp", ""),
    ("gaz_se", "exp", ""),
)


@dataclass(frozen=True)
class _CarrierMix:
    """How to compute the renewable share of one energy carrier.

    ``targets``
        The nodes whose *inflows* make up the carrier's supply mix.
    ``renewable``
        Source nodes counted as renewable.  Imports are counted as renewable,
        which is the legacy convention for hydrogen, ammonia and methanol; see
        the note in the final report.
    ``other``
        Source nodes counted as non-renewable.  Empty means "discover from the
        flow table" -- every remaining source that actually feeds ``targets``.
        FIX 2 relies on this: the four ratios whose denominator merely repeated
        the numerator now discover theirs.
    """

    carrier: str
    targets: tuple[str, ...]
    renewable: tuple[str, ...]
    other: tuple[str, ...] = ()


#: The renewable share of each final carrier.  The first five reproduce the
#: legacy denominators exactly; the last four had none worth reproducing.
RENEWABLE_SHARES: tuple[_CarrierMix, ...] = (
    _CarrierMix(
        "elc_fe",
        ("elc_se",),
        renewable=("spv_pe", "eon_pe", "eof_pe", "hdr_pe", "enc_pe"),
        other=("pac_pe", "cms_pe", "gaz_pe", "pet_pe", "ura_pe"),
    ),
    _CarrierMix(
        "gaz_fe",
        ("gaz_se",),
        renewable=("bgl_pe", "enc_pe", "hyd_se"),
        other=("gaz_pe",),
    ),
    _CarrierMix(
        "pet_fe",
        ("pet_fe", "lqf_se"),
        renewable=("enc_pe", "hyd_se"),
        other=("pet_pe",),
    ),
    _CarrierMix(
        "hyd_fe",
        ("hyd_se",),
        renewable=("elc_se", "imp"),
        other=("gaz_se",),
    ),
    _CarrierMix(
        "vap_fe",
        ("vap_se",),
        renewable=("enc_pe", "bgl_pe", "elc_se", "pac_pe", "hyd_se", "tes_se"),
        other=("cms_pe", "pet_pe", "gaz_se"),
    ),
    _CarrierMix("enc_fe", ("enc_fe",), renewable=("enc_pe",)),
    _CarrierMix("pac_fe", ("pac_fe",), renewable=("pac_pe",)),
    _CarrierMix("amm_fe", ("amm_fe",), renewable=("elc_se", "hyd_se", "imp")),
    _CarrierMix("met_fe", ("met_fe",), renewable=("elc_se", "hyd_se", "imp")),
)


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

FlowKey = tuple[str, str, str]


@dataclass(frozen=True)
class SelfSufficiency:
    """Primary-energy and electricity self-sufficiency for one spatial unit.

    ``ratio``
        Percent, columns ``primary`` and ``electricity``.  Not clipped: a net
        exporter is above 100.  ``NaN`` when the denominator is not positive.
    ``primary`` / ``electricity``
        TWh/year balances, columns ``domestic`` and ``net_import`` (imports
        minus exports; negative means net exporter).
    """

    ratio: pd.DataFrame
    primary: pd.DataFrame
    electricity: pd.DataFrame


def _zeros(index: pd.Index) -> pd.Series:
    return pd.Series(0.0, index=index, dtype=float)


def column(frame: pd.DataFrame, key: FlowKey | str) -> pd.Series:
    """One column of ``frame`` as a float Series, or zeros when it is absent.

    Replaces both the bare ``flows[key]`` lookups (which raised ``KeyError`` as
    soon as a model lacked a technology) and the ``.squeeze().rename_axis(None)``
    incantation the original needed because its columns were not unique.
    """
    if key not in frame.columns:
        return _zeros(frame.index)
    value = frame[key]
    if isinstance(value, pd.DataFrame):  # cannot happen post-FIX 6; belt and braces
        value = value.sum(axis=1)
    return pd.to_numeric(value, errors="coerce").fillna(0.0).astype(float)


def sum_by(
    frame: pd.DataFrame,
    *,
    where: str,
    nodes: Iterable[str],
    by: str,
) -> pd.DataFrame:
    """Sum the flows whose ``where`` level is in ``nodes``, grouped by ``by``.

    The one operation the legacy code spelled out eleven times as
    ``flows.loc[:, flows.columns.get_level_values(f).isin(l)].groupby(level=g,
    axis=1).sum()``.  ``groupby(axis=1)`` was removed in pandas 3, hence the
    transpose.
    """
    wanted = set(nodes)
    mask = frame.columns.get_level_values(where).isin(wanted)
    if not mask.any():
        return pd.DataFrame(index=frame.index)
    return frame.loc[:, mask].T.groupby(level=by).sum().T


def node_flows(
    frame: pd.DataFrame,
    nodes: str | Sequence[str],
    *,
    direction: str,
    split: str,
) -> pd.DataFrame:
    """Flows into or out of ``nodes``, split by their other endpoint.

    ``direction`` is ``'in'`` or ``'out'``; ``split`` is ``'source'``,
    ``'target'`` or ``'node'``.  The legacy ``sf.node_consumption`` took
    ``direction='forward' | 'backward' | 'backwards'`` and a ``splitby`` whose
    meaning was the *opposite* of its name (``splitby='target'`` grouped by
    ``Source``), which is why three of its five call sites read as bugs and are
    not.
    """
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    if split not in ("source", "target", "node"):
        raise ValueError(f"split must be 'source', 'target' or 'node', got {split!r}")

    # 'source' and 'target' name the level to group by; 'node' keeps the
    # selected nodes themselves as the columns.
    endpoint = "Source" if direction == "out" else "Target"
    by = endpoint if split == "node" else split.capitalize()

    node_list = [nodes] if isinstance(nodes, str) else list(nodes)
    result = sum_by(frame, where=endpoint, nodes=node_list, by=by)
    if split == "node":
        keep = [n for n in node_list if n in result.columns]
        result = result[keep]
    return result


def _share_percent(frame: pd.DataFrame, base: float = 100.0) -> pd.DataFrame:
    """Row-wise shares of ``frame``, in percent (``sf.share_percent``)."""
    total = frame.sum(axis=1)
    total = total.where(total != 0, np.nan)
    return frame.div(total, axis=0).fillna(0.0) * base


def _endpoint_sum(frame: pd.DataFrame, level: str, code: str) -> pd.Series:
    """Sum every flow whose ``level`` (Source/Target/Type) equals ``code``."""
    mask = frame.columns.get_level_values(level) == code
    if not mask.any():
        return _zeros(frame.index)
    return frame.loc[:, mask].sum(axis=1).astype(float)


def nuclear_primary_mode(ctx) -> str:
    """How nuclear counts in primary-energy independence.

    Default ``uranium``: fuel is an import (not mined here). ``electricity``:
    kWh generated inside the node count as domestic. Invalid values raise.
    """
    features = getattr(ctx.config, "features", None)
    raw = getattr(features, "nuclear_primary", None)
    if raw is None:
        raw = (getattr(ctx.config, "raw", {}) or {}).get("features", {}).get(
            "nuclear_primary"
        )
    mode = str(raw or NUCLEAR_PRIMARY_URANIUM).strip().lower()
    if mode not in NUCLEAR_PRIMARY_LABELS:
        raise ValueError(
            "features.nuclear_primary must be 'uranium' or 'electricity', "
            f"got {raw!r}"
        )
    return mode


def graph_imbalances(
    flows: pd.DataFrame,
    taxonomy,
    *,
    atol: float = 0.05,
    rtol: float = 0.02,
) -> pd.DataFrame:
    """Transformation nodes whose annual inflow does not equal outflow.

    ``atol`` is TWh (or MtCO2 on a carbon graph); ``rtol`` is relative to
    ``max(|in|, |out|)``.  Both must be exceeded, so a 0.03 TWh rounding
    residual on a 10 TWh node is ignored and a 4 TWh hole on a 5 TWh node
    is not.

    An empty frame means every primary / secondary / final node conserves
    energy.  That is the test the energy Sankey must pass: a BEV node with
    more leaving than arriving is this function's original reason to exist.
    """
    columns = ["node", "year", "incoming", "outgoing", "gap"]
    if flows is None or flows.empty:
        return pd.DataFrame(columns=columns)
    types = taxonomy.nodes["Type"]
    codes = [c for c in types.index if types.get(c) in BALANCE_NODE_TYPES]
    incoming = sum_by(flows, where="Target", nodes=codes, by="Target")
    outgoing = sum_by(flows, where="Source", nodes=codes, by="Source")
    rows: list[dict] = []
    for code in codes:
        inn = (
            incoming[code].reindex(flows.index).fillna(0.0)
            if code in incoming.columns
            else _zeros(flows.index)
        )
        out = (
            outgoing[code].reindex(flows.index).fillna(0.0)
            if code in outgoing.columns
            else _zeros(flows.index)
        )
        gap = out - inn
        scale = np.maximum(inn.abs(), out.abs())
        for year in flows.index:
            g = float(gap.loc[year])
            s = float(scale.loc[year])
            if abs(g) <= atol:
                continue
            if s > 0 and abs(g) / s <= rtol:
                continue
            rows.append(
                {
                    "node": code,
                    "year": year,
                    "incoming": float(inn.loc[year]),
                    "outgoing": float(out.loc[year]),
                    "gap": g,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def _warn_imbalances(ctx, node: str, flows: pd.DataFrame) -> None:
    """Log unbalanced Sankey nodes; never raises (the report still renders)."""
    imb = graph_imbalances(flows, ctx.taxonomy)
    if imb.empty:
        return
    parts = [
        f"{row.node} {row.year} {row.gap:+.2f} (in {row.incoming:.2f}, out {row.outgoing:.2f})"
        for row in imb.itertuples(index=False)
    ]
    extra = " ..." if len(parts) > 12 else ""
    logger.warning(
        "energy Sankey for %s: %d unbalanced transformation node-year(s) "
        "(outgoing − incoming, TWh): %s%s",
        node,
        len(parts),
        "; ".join(parts[:12]),
        extra,
    )


def _sufficiency_ratio(domestic: pd.Series, net_import: pd.Series) -> pd.Series:
    """``100 * domestic / (domestic + net_import)``, ``NaN`` if supply ≤ 0.

    ``net_import`` is imports minus exports, so a net exporter exceeds 100 %.
    The legacy coverage ratios clipped at 100, which made exporters
    indistinguishable from autarky.
    """
    supply = domestic + net_import
    return (100.0 * domestic / supply).where(supply > 0)


def _sum_long_tables(tables: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Add long flow tables that share ``code`` / ``label`` / ``unit``."""
    frames = [t for t in tables if t is not None and not t.empty]
    if not frames:
        return pd.DataFrame(columns=["code", "label", "unit"])
    if len(frames) == 1:
        return frames[0].copy()
    pieces = []
    for table in frames:
        extra = [c for c in table.columns if c not in ("code", "label", "unit")]
        piece = table.set_index("code")
        meta = piece[["label", "unit"]]
        values = piece[extra].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        pieces.append(pd.concat([meta, values], axis=1))
    stacked = pd.concat(pieces, axis=0)
    values = stacked.drop(columns=["label", "unit"]).groupby(level=0).sum()
    meta = stacked[["label", "unit"]].groupby(level=0).first()
    out = meta.join(values).reset_index()
    return out[["code", "label", "unit", *values.columns]]


def _interpolate_to(series: pd.Series, years: Sequence[int]) -> pd.Series:
    """Put an exogenous per-year series on the report's horizons.

    The packaged production tables are quoted for 2020/2030/2040/2050; a model
    solved for 2025 or 2035 needs a value there too.  The legacy code matched
    the year labels as strings and produced NaN for anything else.
    """
    numeric = pd.Series(
        {int(k): float(v) for k, v in series.items() if str(k).strip().lstrip("-").isdigit()}
    )
    if numeric.empty:
        return _zeros(pd.Index(years))
    full = numeric.reindex(sorted(set(numeric.index) | set(years)))
    full = full.interpolate(method="index").ffill().bfill()
    return full.reindex(years).astype(float)


# ---------------------------------------------------------------------------
# Long table -> wide value table -> graph
# ---------------------------------------------------------------------------


def flows_from_long(df: pd.DataFrame, year_columns: Sequence[str]) -> pd.DataFrame:
    """``(code, label, unit, <year>...)`` -> ``index=year, columns=code``.

    This is the single entry point for both data paths: the extraction layer's
    :func:`pypsa2html.extract.energy_flows` output, and -- for regression
    testing against the shipped results -- the legacy ``inputs<node>.xlsx``
    sheets, whose ``target`` column is the ``code`` and whose ``source`` column
    is the ``unit``.

    The index is *integer* years.  The legacy frame used the strings ``'2020'``
    ... ``'2050'``, which is why its Sankey code is littered with ``str(year)``
    and why its x axes were categorical.
    """
    if df is None or df.empty:
        return pd.DataFrame(index=pd.Index([int(y) for y in year_columns], name="Year"))

    frame = df.copy()
    if "code" not in frame.columns:
        raise ValueError(
            "the long flow table needs a 'code' column; got "
            f"{list(frame.columns)}. See docs/INTERNALS.md section 3."
        )

    present = [c for c in year_columns if c in frame.columns]
    missing = [c for c in year_columns if c not in frame.columns]
    if missing:
        logger.warning("flow table has no column for year(s) %s; filled with 0", missing)

    values = frame.set_index("code")[present].apply(pd.to_numeric, errors="coerce")
    # A code repeated in the input is summed rather than silently dropped, as
    # `data.loc[:, ~data.columns.duplicated()]` did in the original.
    values = values.groupby(level=0).sum()

    wide = values.T
    wide.index = pd.Index([int(y) for y in present], name="Year")
    for year in missing:
        wide.loc[int(year)] = 0.0
    return wide.sort_index().astype(float).fillna(0.0)


def _graph(
    values: pd.DataFrame,
    processes: pd.DataFrame,
    rename: dict[str, str],
    *,
    what: str,
) -> pd.DataFrame:
    """Attach a wide value table to the edges declared in a processes table.

    Codes referenced by an edge but absent from the model output are filled
    with zero -- a technology a scenario does not build is not an error.
    """
    data = values.rename(columns=rename)
    data = data.loc[:, ~data.columns.duplicated()]

    wanted = processes["Value_Code"].dropna().unique()
    unfound = sorted(set(wanted) - set(data.columns))
    if unfound:
        logger.info(
            "%s: %d indicator(s) absent from the model output, filled with 0 (%s%s)",
            what,
            len(unfound),
            ", ".join(unfound[:8]),
            " ..." if len(unfound) > 8 else "",
        )
        data = data.reindex(columns=[*data.columns, *unfound], fill_value=0.0)

    edges = processes[processes["Value_Code"].isin(data.columns)]
    columns = pd.MultiIndex.from_arrays(
        [edges["Source"].to_numpy(), edges["Target"].to_numpy(), edges["Type"].to_numpy()],
        names=("Source", "Target", "Type"),
    )
    flows = pd.DataFrame(
        data[edges["Value_Code"]].to_numpy(dtype=float),
        index=data.index,
        columns=columns,
    )
    # FIX 6: collapse the duplicate (Source, Target, Type) edges.
    return flows.T.groupby(level=["Source", "Target", "Type"]).sum().T


# ---------------------------------------------------------------------------
# Exogenous inputs
# ---------------------------------------------------------------------------


def _real_codes(ctx) -> list[str]:
    return list(ctx.nodes.real_codes)


def _study_wide(ctx, node: str) -> bool:
    """Closed-system graph algebra applies only to the study-wide sum."""
    if hasattr(ctx, "is_study_wide"):
        return ctx.is_study_wide(node)
    return ctx.is_aggregate(node)


def _table_codes(ctx, node: str, index) -> list[str]:
    """Index labels to sum in a per-region table for ``node``."""
    members = ctx.members_of(node) if hasattr(ctx, "members_of") else None
    if ctx.is_aggregate(node) and members is None:
        return [c for c in _real_codes(ctx) if c in index]
    wanted = members if members is not None else [node]
    present = [c for c in wanted if c in index]
    if present:
        return present
    if node in index:
        return [node]
    return []


def _domestic_production(ctx, node: str, table: pd.DataFrame, years, what: str) -> pd.Series:
    """Exogenous domestic fossil production for ``node``, in TWh/year.

    Replaces ``LOCAL_GAS.loc[country]`` / ``LOCAL_OIL.loc[country]``, which
    raised ``KeyError`` for any node not in the packaged table -- every
    sub-national node of pypsa-wal, for instance.
    """
    codes = _table_codes(ctx, node, table.index)
    if not codes:
        logger.warning(
            "%s: no region of %s appears in the table; assuming no domestic production for node %s",
            what,
            _real_codes(ctx),
            node,
        )
        return _zeros(pd.Index(years))
    rows = table.reindex(codes).dropna(how="all")
    if rows.empty:
        logger.warning(
            "%s: no region of %s appears in the table; assuming no domestic production for node %s",
            what,
            codes,
            node,
        )
        return _zeros(pd.Index(years))
    return _interpolate_to(rows.sum(numeric_only=True), years)


def _region_value(totals: pd.Series, node: str) -> float:
    """Look a node up in a per-bus series, tolerating a clustering suffix.

    Exact match first.  Clustered names look like ``BE1 0`` for node ``BE``
    — a digit after the code — and never ``BEWAL`` (no prefix matching).
    """
    if node in totals.index:
        return float(totals.loc[node])
    names = totals.index.astype(str)
    clustered = totals[names.str.match(rf"^{re.escape(node)}\d")]
    if len(clustered):
        return float(clustered.sum())
    return float("nan")


def biomass_potential(ctx, node: str, years: Sequence[int]) -> pd.Series | None:
    """Domestic solid-biomass potential per horizon, in TWh/year.

    Port of the legacy biomass-potentials helper.  ``None`` when no potential file is
    available at all, in which case the caller keeps the model's own domestic
    production; a horizon whose file is missing (négaWatt ships none for its
    calibration year) yields NaN and is filled the same way.
    """
    clusters = ctx.config.model.clusters
    values: dict[int, float] = {}
    for year in years:
        raw = ctx.read_csv(
            f"biomass_potentials_s_{clusters}_{year}.csv", base="resources", index_col=0
        )
        if raw is None:
            continue
        available = [c for c in BIOMASS_POTENTIAL_COLUMNS if c in raw.columns]
        if not available:
            logger.warning(
                "biomass_potentials_s_%s_%s.csv has none of the columns %s",
                clusters,
                year,
                list(BIOMASS_POTENTIAL_COLUMNS),
            )
            continue
        totals = raw[available].sum(axis=1) / 1e6  # MWh -> TWh
        totals = totals.groupby(totals.index).sum()
        members = ctx.members_of(node) if hasattr(ctx, "members_of") else None
        if ctx.is_aggregate(node) and members is None:
            lookup = _real_codes(ctx)
        elif members is not None:
            lookup = members
        else:
            lookup = [node]
        selected = [_region_value(totals, code) for code in lookup]
        if np.isnan(selected).all() and node not in lookup:
            values[year] = _region_value(totals, node)
        else:
            values[year] = float(np.nansum(selected))

    if not values:
        logger.info(
            "no biomass_potentials_s_%s_*.csv under %s; keeping the model's own "
            "domestic biomass production",
            clusters,
            ctx.resources_dir,
        )
        return None
    return pd.Series(values, dtype=float).reindex(years)


def year_weights(ctx, years: Sequence[int]) -> pd.Series:
    """Years represented by each row of a flow frame (FIX 4).

    ``ctx.horizon_weights()`` covers the modelled horizons; a base year merged
    in from an exogenous source is not one of them, so its weight is derived
    from the gap to the next year present.
    """
    base = ctx.horizon_weights()
    years = list(years)
    if all(y in base.index for y in years):
        return base.reindex(years).astype(float)

    ordered = sorted(years)
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    derived = pd.Series(gaps + [gaps[-1] if gaps else 1.0], index=ordered, dtype=float)
    return base.reindex(ordered).fillna(derived).reindex(years).astype(float)


# ---------------------------------------------------------------------------
# The energy graph
# ---------------------------------------------------------------------------


def _measured_electricity_trade(ctx, node: str, years) -> tuple[pd.Series, pd.Series] | None:
    """Real cross-border electricity for ``node``, TWh/year, or ``None``.

    The hourly **positive and negative parts** of the node's net exchange over
    AC lines and DC links, snapshot-weighted — the same quantity
    :func:`pypsa2html.extract.balance.import_export_series` puts in the energy
    balance, and the one an import cap constrains.

    It replaces an annual-net residual (see :func:`_close_energy_graph` step 8).
    That residual was wrong twice over: a region both imports and exports within
    a year and the net hides one of them, and — because it was whatever closed
    the ``elc_se`` node — it silently absorbed every mis-attribution elsewhere
    on that node. On BEWAL 2050 of the 2026-09-05 run it drew **17.2 TWh** of
    imports and zero exports against a physical 10.0 in / 2.0 out. See D20.
    """
    from .extract.balance import import_export_series

    if not hasattr(ctx, "networks"):
        return None
    locations = (
        ctx.locations_for(node)
        if hasattr(ctx, "locations_for")
        else (None if ctx.is_aggregate(node) else [node])
    )
    imports, exports = _zeros(years), _zeros(years)
    seen = False
    for year in years:
        try:
            n = ctx.networks[int(year)]
        except Exception:  # noqa: BLE001 - a missing horizon is not fatal
            continue
        try:
            net = import_export_series(n, ctx.resolver(int(year)), locations)
        except Exception as err:  # noqa: BLE001 - never break the report
            logger.warning("electricity trade for %s @ %s: %s", node, year, err)
            return None
        weights = n.snapshot_weightings.generators
        imports.loc[year] = float(net.clip(lower=0).mul(weights).sum()) / 1e6
        exports.loc[year] = float((-net.clip(upper=0)).mul(weights).sum()) / 1e6
        seen = True
    return (imports, exports) if seen else None


def _close_energy_graph(ctx, node: str, flows: pd.DataFrame) -> pd.DataFrame:
    """Derive the flows the model does not report directly.

    Port of the legacy energy-graph closure.  ``flows`` is modified and
    returned; it is a frame this module built, never a caller's argument.
    """
    tax = ctx.taxonomy
    fe = tax.codes_of_type("FINAL_ENERGIES")
    se = tax.codes_of_type("SECONDARY_ENERGIES")
    pe = tax.codes_of_type("PRIMARY_ENERGIES")
    years = flows.index
    aggregate = _study_wide(ctx, node)

    def fe_consumption() -> pd.DataFrame:
        return sum_by(flows, where="Source", nodes=fe, by="Source")

    # -- 1. deliveries from the networks to final use ----------------------
    fec = fe_consumption()
    for carrier in SE_TO_FE_CARRIERS:
        flows[(f"{carrier}_se", f"{carrier}_fe", "")] = fec.get(f"{carrier}_fe", _zeros(years))

    # -- 2. liquid fuels: fossil oil is the non-synthetic remainder --------
    demand = fec.get("pet_fe", _zeros(years))
    fischer = column(flows, ("hyd_se", "pet_fe", ""))
    biomass_liquid = column(flows, ("enc_pe", "pet_fe", ""))
    electro_biofuel = column(flows, ("enc_pe", "pet_fe", "bio"))
    synthetic = fischer + biomass_liquid + electro_biofuel
    flows[("pet_pe", "pet_fe", "")] = (demand - synthetic).where(
        demand > synthetic, fischer - biomass_liquid - demand
    )

    base_year = ctx.config.model.base_year
    if base_year is not None and base_year in years:
        # A calibration year taken from statistics has no modelled synthetic
        # fuels, so all of its liquid demand is fossil.  The legacy code
        # hardcoded this for 2020; pypsa-wal dropped it with its base year.
        flows.loc[base_year, ("pet_pe", "pet_fe", "")] = demand.loc[base_year]

    # NB: the pypsa-wal fork subtracts the electricity distribution losses
    # (``elc_se -> per`` type ``dis``) from the residential electricity demand
    # here, on the grounds that they are counted twice.  That is *not* ported:
    # with the shared extraction both models use, those losses are booked on
    # the grid node only, and subtracting them makes the final-electricity node
    # lose exactly that much (verified on the négaWatt reference: elc_fe is
    # balanced to 1e-12 without the correction and short by 3.7-3.8 TWh with
    # it).  If a model really does report the losses inside the demand, the
    # place to fix it is the extraction layer, before step 1 sets the delivery.

    # -- 3. deliveries straight from a primary energy ----------------------
    fec = fe_consumption()
    for carrier in PE_TO_FE_CARRIERS:
        flows[(f"{carrier}_pe", f"{carrier}_fe", "")] = fec.get(f"{carrier}_fe", _zeros(years))

    # -- 4. fossil gas is the gas grid minus its renewable injections ------
    se_consumption = sum_by(flows, where="Source", nodes=se, by="Source")
    renewable_gas = sum(
        (column(flows, key).clip(lower=0) for key in GAS_GRID_RENEWABLE), _zeros(years)
    )
    flows[("gaz_pe", "gaz_se", "")] = se_consumption.get("gaz_se", _zeros(years)) - renewable_gas

    # -- 5. nuclear thermal losses from the reactor efficiency -------------
    efficiency = ctx.cost("nuclear", "efficiency", default=float("nan"))
    if efficiency and np.isfinite(efficiency) and efficiency > 0:
        flows[("ura_pe", "per", "thm")] = column(flows, ("ura_pe", "elc_se", "thm")) * (
            (1 - efficiency) / efficiency
        )
    else:
        logger.warning("no nuclear efficiency in the cost table; nuclear thermal losses omitted")

    # -- 6. domestic production versus imports -----------------------------
    pe_supply = sum_by(flows, where="Source", nodes=pe, by="Source")
    domestic = list(DOMESTIC_CARRIERS) + (["enc"] if aggregate else [])
    for carrier in domestic:
        flows[("prod", f"{carrier}_pe", "")] = pe_supply.get(f"{carrier}_pe", _zeros(years))
    for carrier in IMPORTED_CARRIERS:
        flows[("imp", f"{carrier}_pe", "")] = pe_supply.get(f"{carrier}_pe", _zeros(years))

    for carrier, table, what in (
        ("gaz", ctx.taxonomy.domestic_gas, "domestic gas production"),
        ("pet", ctx.taxonomy.domestic_oil, "domestic oil production"),
    ):
        used = pe_supply.get(f"{carrier}_pe", _zeros(years))
        local = _domestic_production(ctx, node, table, years, what)
        flows[("prod", f"{carrier}_pe", "")] = np.minimum(used, local)
        flows[("imp", f"{carrier}_pe", "")] = (used - local).clip(lower=0)

    # -- 7. biomass: exogenous potential versus modelled consumption -------
    if not aggregate:
        potential = biomass_potential(ctx, node, list(years))
        if potential is not None:
            # A horizon with no potential file keeps the model's own value;
            # the négaWatt study ships none for its calibration year and the
            # original patched that up with a hardcoded ``.loc['2020']``.
            potential = potential.fillna(column(flows, ("prod", "enc_pe", "")))
            used = pe_supply.get("enc_pe", _zeros(years))
            flows[("prod", "enc_pe", "")] = potential
            # Clipped, like the elc/hyd balances below.  The original left both
            # signed, so one of the pair was always a negative flow: invisible
            # in the Sankey, which filters ``> 0``, but not in the primary
            # energy totals, where a negative export subtracted real supply.
            flows[("imp", "enc_pe", "")] = (used - potential).clip(lower=0)
            flows[("enc_pe", "exp", "")] = (potential - used).clip(lower=0)

    # -- 8. trade in secondary carriers ------------------------------------
    if not aggregate:
        produced = sum_by(flows, where="Target", nodes=se, by="Target")
        consumed = sum_by(flows, where="Source", nodes=se, by="Source")
        # Electricity is measured, not inferred: the model carries the actual
        # cross-border branches, so there is no reason to read trade off a
        # residual that also absorbs every mapping error on the node. Any
        # remaining gap is now left visible for `graph_imbalances` to report,
        # which is the point of that check. D20.
        measured = _measured_electricity_trade(ctx, node, years)
        for carrier in TRADED_SE_CARRIERS:
            code = f"{carrier}_se"
            if code == "elc_se" and measured is not None:
                flows[("imp", code, "")], flows[(code, "exp", "")] = measured
                continue
            balance = consumed.get(code, _zeros(years)) - produced.get(code, _zeros(years))
            flows[("imp", code, "")] = balance.clip(lower=0)
            flows[(code, "exp", "")] = (-balance).clip(lower=0)

    # -- 9. trade in final carriers, and their synthesis losses -----------
    produced = sum_by(flows, where="Target", nodes=fe, by="Target")
    consumed = fe_consumption()
    for carrier in TRADED_FE_CARRIERS:
        code = f"{carrier}_fe"
        # FIX 1: the target was hardcoded to ('met_fe', 'per', '').
        electricity = column(flows, ("elc_se", code, ""))
        flows[(code, "per", "")] = electricity
        if aggregate:
            continue
        supply = produced.get(code, _zeros(years))
        demand = consumed.get(code, _zeros(years))
        flows[("imp", code, "")] = (demand - supply - electricity).clip(lower=0)
        flows[(code, "exp", "")] = (supply - demand - electricity).clip(lower=0)

    # -- 10. rural heat pumps exclude the agricultural share ---------------
    flows[("pac_fe", "res", "gr")] = column(flows, ("pac_fe", "res", "gr")) - column(
        flows, ("pac_fe", "agr", "")
    )

    return flows


def _close_carbon_graph(
    ctx, node: str, flows_co2: pd.DataFrame, flows: pd.DataFrame
) -> pd.DataFrame:
    """Derive the carbon flows the model does not report directly.

    Port of the legacy carbon-graph closure.  The per-year ``.loc['2030']
    ... .loc['2050']`` chains are vectorised (FIX 5), which incidentally gives
    the base year a value where the négaWatt build left NaN.
    """
    tax = ctx.taxonomy
    ghg_sectors = tax.codes_of_type("GHG_SECTORS")
    pe = tax.codes_of_type("PRIMARY_ENERGIES")
    fe = tax.codes_of_type("FINAL_ENERGIES")
    years = flows_co2.index
    aggregate = _study_wide(ctx, node)

    pe_supply = sum_by(flows, where="Source", nodes=pe, by="Source")
    fe_use = sum_by(flows, where="Source", nodes=fe, by="Source")

    gas_intensity = ctx.cost("gas", "CO2 intensity", default=float("nan"))
    methanol_co2 = ctx.cost("methanolisation", "carbondioxide-input", default=float("nan"))

    # -- fossil gas emissions, from the gas actually burnt -----------------
    if not aggregate and np.isfinite(gas_intensity):
        flows_co2[("fgs_ghg", "gas_ghg", "")] = (
            pe_supply.get("gaz_pe", _zeros(years)) * gas_intensity
        )

    # -- net emissions: gross minus the biogenic and land-use sinks --------
    inflows = sum_by(flows_co2, where="Target", nodes=ghg_sectors, by="Target")
    flows_co2[("atm", "net_ghg", "net")] = (
        inflows.get("atm", _zeros(years))
        - inflows.get("bm_ghg", _zeros(years))
        - inflows.get("luf_ghg", _zeros(years))
        - column(flows_co2, ("atm", "stm", ""))
        - column(flows_co2, ("atm", "blg_ghg", ""))
        - column(flows_co2, ("atm", "blg_ghg", "cc"))
    )

    # -- methanol trade carries its embodied CO2 ---------------------------
    if not aggregate and np.isfinite(methanol_co2):
        demand = fe_use.get("met_fe", _zeros(years))
        produced = column(flows, ("hyd_se", "met_fe", ""))
        flows_co2[("hth_ghg", "met_ghg", "")] = (demand - produced) * methanol_co2
        flows_co2[("met_ghg", "eth_ghg", "")] = (produced - demand) * methanol_co2

    # -- the fossil-oil node collects every oil combustion emission --------
    if not aggregate:
        combustion = sum(
            (
                column(flows_co2, ("oil_ghg", "atm", kind))
                for kind in ("rb", "rc", "oi", "hvc", "agr", "avi", "so", "tra")
            ),
            _zeros(years),
        )
        flows_co2[("fol_ghg", "oil_ghg", "")] = (
            combustion
            + column(flows_co2, ("oil_ghg", "hvc_ghg", "oil"))
            - column(flows_co2, ("stm", "oil_ghg", ""))
            - column(flows_co2, ("bm_ghg", "oil_ghg", "bio"))
            # pypsa-wal: biomass-to-liquid; absent from the négaWatt taxonomy.
            - column(flows_co2, ("bm_ghg", "oil_ghg", "bm"))
        )

    # -- for the aggregate node, fossil gas is the residual ----------------
    if aggregate:
        outflows = sum_by(flows_co2, where="Source", nodes=ghg_sectors, by="Source")
        flows_co2[("fgs_ghg", "gas_ghg", "")] = (
            outflows.get("gas_ghg", _zeros(years))
            - column(flows_co2, ("blg_ghg", "gas_ghg", ""))
            - column(flows_co2, ("blg_ghg", "gas_ghg", "cc"))
            - column(flows_co2, ("bm_ghg", "gas_ghg", ""))
            - column(flows_co2, ("bm_ghg", "gas_ghg", "cc"))
            - column(flows_co2, ("stm", "gas_ghg", ""))
        )

    return flows_co2


# ---------------------------------------------------------------------------
# The indicator set
# ---------------------------------------------------------------------------


@dataclass
class Indicators:
    """Every frame the indicator chart builders need, for one node.

    Members are :func:`functools.cached_property`, so a section switched off in
    the manifest costs nothing -- unlike the original, where the ``else:``
    branch of each plot toggle still built the full figure and discarded it.
    """

    ctx: object
    node: str
    #: Energy flows, columns ``(Source, Target, Type)``, index integer years.
    flows: pd.DataFrame
    #: Carbon flows for the carbon Sankey, same shape.
    flows_co2: pd.DataFrame
    #: Greenhouse-gas attribution flows, same shape.
    flows_ghg: pd.DataFrame

    # -- basic vocabulary --------------------------------------------------
    @property
    def years(self) -> list[int]:
        return [int(y) for y in self.flows.index]

    @property
    def _tax(self):
        return self.ctx.taxonomy

    # -- final energy consumption ------------------------------------------
    @cached_property
    def fec_carrier(self) -> pd.DataFrame:
        """Final consumption by energy carrier."""
        return sum_by(
            self.flows,
            where="Source",
            nodes=self._tax.codes_of_type("FINAL_ENERGIES"),
            by="Source",
        )

    @cached_property
    def fec_sector(self) -> pd.DataFrame:
        """Final consumption by demand sector."""
        return sum_by(
            self.flows,
            where="Target",
            nodes=self._tax.codes_of_type("DEMAND_SECTORS"),
            by="Target",
        )

    @cached_property
    def fec_by_sector(self) -> dict[str, pd.DataFrame]:
        """Per final carrier: which sectors consume it."""
        return {
            code: node_flows(self.flows, code, direction="out", split="target")
            for code in self._tax.codes_of_type("FINAL_ENERGIES")
        }

    @cached_property
    def fec_by_carrier(self) -> dict[str, pd.DataFrame]:
        """Per demand sector: which carriers it consumes."""
        return {
            code: node_flows(self.flows, code, direction="in", split="source")
            for code in self._tax.codes_of_type("DEMAND_SECTORS")
        }

    @cached_property
    def sec_mix(self) -> dict[str, pd.DataFrame]:
        """Per secondary carrier: what produces it."""
        return {
            code: node_flows(self.flows, code, direction="in", split="source")
            for code in self._tax.codes_of_type("SECONDARY_ENERGIES")
        }

    # -- primary energy ----------------------------------------------------
    @cached_property
    def pec(self) -> pd.DataFrame:
        """Primary energy supply by carrier."""
        return sum_by(
            self.flows,
            where="Source",
            nodes=self._tax.codes_of_type("PRIMARY_ENERGIES"),
            by="Source",
        )

    @cached_property
    def gfec_breakdown(self) -> pd.DataFrame:
        """Primary supply folded into renewable / fossil / nuclear.

        The legacy code hardcoded the three membership lists and forgot
        ``win_pe``, ``wst_ren_pe`` and ``wst_fos_pe``, so an aggregated wind
        node was excluded from the renewable total.  The membership is a per
        technology lookup, which by ``docs/INTERNALS.md`` section 6.6 belongs in
        a CSV -- and it already is one: the ``Category`` column of
        ``data/nodes.csv``.  Carriers with no category keep their own column,
        exactly as the uncategorised ones did before.
        """
        supply = self.pec
        if supply.empty:
            return supply
        category = self._tax.nodes["Category"].reindex(supply.columns)
        groups = category.where(category.isin(["ren", "fos", "nuk"]))
        grouping = groups.fillna(pd.Series(supply.columns, index=supply.columns))
        folded = supply.T.groupby(grouping).sum().T
        for name in ("ren", "fos", "nuk"):
            if name in folded.columns:
                folded[name] = folded[name].clip(lower=0)
        return folded.loc[:, (folded != 0).any()]

    # -- coverage ratios ---------------------------------------------------
    @cached_property
    def cov_ratio(self) -> pd.DataFrame:
        """Share of each carrier's consumption covered by domestic supply, %."""
        tax = self._tax
        years = self.flows.index
        exports = sum_by(
            self.flows, where="Target", nodes=tax.codes_of_type("EXPORTS"), by="Source"
        )
        imports = sum_by(
            self.flows, where="Source", nodes=tax.codes_of_type("IMPORTS"), by="Target"
        )
        traded = sorted(set(imports.columns) | set(exports.columns))

        consumption = sum_by(self.flows, where="Target", nodes=BALANCE_TARGETS, by="Target")
        absent = [c for c in BALANCE_TARGETS if c not in consumption.columns]
        if absent:
            logger.debug("no inflow to %s; excluded from the coverage ratios", absent)

        numerator = consumption.subtract(imports, fill_value=0).filter(traded)
        denominator = consumption.subtract(exports, fill_value=0).filter(traded)
        ratios = 100 * numerator / denominator

        # Gas and liquid fuels are reported as a *renewable* coverage instead.
        renewable_gas = sum((column(self.flows, key) for key in GAS_GRID_RENEWABLE), _zeros(years))
        fossil_gas = column(self.flows, ("gaz_pe", "gaz_se", ""))
        ratios["gaz_se"] = 100 * renewable_gas / (fossil_gas + renewable_gas)

        renewable_liquid = (
            column(self.flows, ("enc_pe", "pet_fe", ""))
            + column(self.flows, ("hyd_se", "pet_fe", ""))
            + column(self.flows, ("enc_pe", "pet_fe", "bio"))
        )
        fossil_liquid = column(self.flows, ("pet_pe", "pet_fe", ""))
        ratios["pet_fe"] = 100 * renewable_liquid / (fossil_liquid + renewable_liquid)
        return ratios.clip(upper=100)

    @cached_property
    def ren_cov_ratio(self) -> pd.DataFrame:
        """Renewable share of each final energy carrier, %.  See FIX 2."""
        ratios = pd.DataFrame(index=self.flows.index)
        sources = self.flows.columns.get_level_values("Source")

        for mix in RENEWABLE_SHARES:
            in_targets = self.flows.columns.get_level_values("Target").isin(mix.targets)
            if not in_targets.any():
                ratios[mix.carrier] = np.nan
                continue
            available = set(sources[in_targets])
            denominator_nodes = set(mix.renewable) | (
                set(mix.other) if mix.other else available - set(mix.renewable)
            )
            selected = in_targets & sources.isin(denominator_nodes)
            supply = self.flows.loc[:, selected].T.groupby(level="Source").sum().T
            renewable = supply.reindex(columns=list(mix.renewable), fill_value=0).sum(axis=1)
            total = supply.sum(axis=1)
            ratios[mix.carrier] = 100 * renewable / total.where(total != 0, np.nan)

        breakdown = _share_percent(self.gfec_breakdown, 100)
        ratios["total"] = breakdown.get("ren", pd.Series(np.nan, index=ratios.index))
        return ratios.clip(upper=100)

    # -- greenhouse gases --------------------------------------------------
    def _signed(self, by: str) -> pd.DataFrame:
        totals = self.flows_ghg.T.groupby(level=by).sum().T
        removals = [c for c in GHG_REMOVALS if c in totals.columns]
        if removals:
            totals[removals] = -totals[removals]
        return totals

    @cached_property
    def ghg_sector(self) -> pd.DataFrame:
        """Emissions by emitting sector; removals carry a negative sign."""
        return self._signed("Source")

    @cached_property
    def ghg_source(self) -> pd.DataFrame:
        """Emissions by emission source; removals carry a negative sign."""
        return self._signed("Target")

    @cached_property
    def _weights(self) -> pd.Series:
        return year_weights(self.ctx, self.years)

    @cached_property
    def ghg_sector_cum(self) -> pd.DataFrame:
        """Cumulative emissions by sector (FIX 3, FIX 4)."""
        return self.ghg_sector.mul(self._weights.to_numpy(), axis=0).cumsum()

    @cached_property
    def ghg_source_cum(self) -> pd.DataFrame:
        """Cumulative emissions by source (FIX 3, FIX 4)."""
        return self.ghg_source.mul(self._weights.to_numpy(), axis=0).cumsum()

    # -- trade summary -----------------------------------------------------
    def _trade(self, keys: Sequence[FlowKey]) -> pd.DataFrame:
        present = [k for k in keys if k in self.flows.columns]
        return pd.DataFrame(
            {f"{k[0]}_{k[1]}": column(self.flows, k) for k in present},
            index=self.flows.index,
        )

    @cached_property
    def imports(self) -> pd.DataFrame:
        return self._trade(IMPORT_FLOWS)

    @cached_property
    def local_production(self) -> pd.DataFrame:
        return self._trade(LOCAL_PRODUCTION_FLOWS)

    @cached_property
    def exports(self) -> pd.DataFrame:
        return self._trade(EXPORT_FLOWS)

    # -- self-sufficiency --------------------------------------------------
    @cached_property
    def self_sufficiency(self) -> SelfSufficiency:
        """Primary-energy and electricity self-sufficiency (see D18).

        Primary energy uses every ``prod`` / ``imp`` / ``exp`` edge on the
        closed energy graph. With the default ``features.nuclear_primary:
        uranium``, uranium is an import (fuel is not mined here) and
        wind/solar/hydro/biomass are domestic. ``electricity`` instead books
        reactor kWh as domestic primary energy and drops the uranium import
        (thermal losses leave the ratio). Electricity self-sufficiency always
        uses the ``elc_se`` balance: nuclear kWh generated inside the node
        count as domestic even though the fuel is imported. Internal trade of
        a group cancels because it is already netted in the residual.
        """
        years = self.flows.index
        domestic_pe = _endpoint_sum(self.flows, "Source", "prod")
        net_pe = _endpoint_sum(self.flows, "Source", "imp") - _endpoint_sum(
            self.flows, "Target", "exp"
        )
        if nuclear_primary_mode(self.ctx) == NUCLEAR_PRIMARY_ELECTRICITY:
            nuc_elc = column(self.flows, ("ura_pe", "elc_se", "thm"))
            ura_imp = column(self.flows, ("imp", "ura_pe", ""))
            domestic_pe = domestic_pe + nuc_elc
            net_pe = net_pe - ura_imp

        elc_in = node_flows(self.flows, "elc_se", direction="in", split="source")
        elc_out = node_flows(self.flows, "elc_se", direction="out", split="target")
        domestic_elc = (
            (
                elc_in.drop(columns=["imp"], errors="ignore").sum(axis=1)
                if len(elc_in.columns)
                else _zeros(years)
            )
            .reindex(years)
            .fillna(0.0)
        )
        imports_elc = (
            (elc_in["imp"] if "imp" in elc_in.columns else _zeros(years)).reindex(years).fillna(0.0)
        )
        exports_elc = (
            (elc_out["exp"] if "exp" in elc_out.columns else _zeros(years))
            .reindex(years)
            .fillna(0.0)
        )
        net_elc = imports_elc - exports_elc

        primary = pd.DataFrame({"domestic": domestic_pe, "net_import": net_pe}, index=years)
        electricity = pd.DataFrame({"domestic": domestic_elc, "net_import": net_elc}, index=years)
        ratio = pd.DataFrame(
            {
                "primary": _sufficiency_ratio(domestic_pe, net_pe),
                "electricity": _sufficiency_ratio(domestic_elc, net_elc),
            },
            index=years,
        )
        return SelfSufficiency(ratio=ratio, primary=primary, electricity=electricity)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def build(ctx, node: str, *, energy: pd.DataFrame, carbon: pd.DataFrame) -> Indicators:
    """Build the indicator set for one node from two long flow tables.

    ``energy`` and ``carbon`` follow ``docs/INTERNALS.md`` section 3.  Neither
    is modified.
    """
    tax = ctx.taxonomy
    rename = tax.rename_map()
    years = ctx.year_columns

    energy_values = flows_from_long(energy, years)
    carbon_values = flows_from_long(carbon, years)

    flows = _graph(energy_values, tax.processes_energy, rename, what="energy flows")
    flows_co2 = _graph(carbon_values, tax.processes_carbon, rename, what="carbon flows")
    flows_ghg = _graph(carbon_values, tax.processes_ghg, rename, what="GHG attribution")

    flows = _close_energy_graph(ctx, node, flows)
    _warn_imbalances(ctx, node, flows)
    flows_co2 = _close_carbon_graph(ctx, node, flows_co2, flows)

    return Indicators(ctx=ctx, node=node, flows=flows, flows_co2=flows_co2, flows_ghg=flows_ghg)


def for_node(ctx, node: str) -> Indicators | None:
    """The indicator set for ``node``, extracted once per build and cached.

    Returns ``None`` -- with a warning, never an exception -- when the
    extraction layer cannot supply the flow tables, so the affected sections
    degrade to a placeholder.
    """
    key = ("indicators", str(node))
    if key in ctx._files:
        return ctx._files[key]

    result = None
    try:
        from . import extract

        energy = extract.energy_flows(ctx, node)
        carbon = extract.carbon_flows(ctx, node)
    except (ImportError, AttributeError) as err:
        logger.warning(
            "the extraction layer does not provide energy_flows/carbon_flows "
            "(%s); indicator sections will be skipped",
            err,
        )
        energy = carbon = None
    except FileNotFoundError as err:
        logger.warning("cannot extract flows for node %s: %s", node, err)
        energy = carbon = None

    if energy is None or carbon is None:
        logger.warning("no flow table for node %s; indicator sections will be skipped", node)
    else:
        result = build(ctx, node, energy=energy, carbon=carbon)

    ctx._files[key] = result
    return result


def _expand_members(ctx, members: Sequence[str]) -> list[str] | None:
    """Real location codes for an ad-hoc ``members`` list.

    Group codes are expanded through :meth:`BuildContext.locations_for`.
    Unknown names are skipped with a warning.  ``None`` if nothing remains.
    """
    wanted: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    real = set(ctx.nodes.real_codes)
    for raw in members:
        code = str(raw)
        if code in seen:
            continue
        if code in real:
            wanted.append(code)
            seen.add(code)
            continue
        try:
            locations = ctx.locations_for(code)
        except KeyError:
            missing.append(code)
            continue
        expanded = ctx.nodes.real_codes if locations is None else list(locations)
        for loc in expanded:
            if loc not in seen:
                wanted.append(loc)
                seen.add(loc)
    if missing:
        logger.warning("self-sufficiency: unknown member(s) %s; skipped", missing)
    if not wanted:
        logger.warning("self-sufficiency: no valid members in %s", list(members))
        return None
    return sorted(wanted)


def _matching_node(ctx, locations: Sequence[str]) -> str | None:
    """Existing node whose membership is exactly ``locations``, if any."""
    target = set(locations)
    for node in ctx.nodes:
        locs = ctx.locations_for(node.code)
        current = set(ctx.nodes.real_codes) if locs is None else set(locs)
        if current == target:
            return node.code
    return None


def _context_for_locations(ctx, locations: Sequence[str]):
    """``(ctx, node)`` whose extraction covers exactly ``locations``.

    Reuses a configured node or group when the membership matches, so a
    ``members=['BEVLG','BEWAL','BEBRU']`` call on a pypsa-wal config with a
    ``BE`` group hits the same cache as ``node='BE'``.  Otherwise a transient
    group is attached to a shallow copy of ``ctx`` (the live NodeSet used for
    page generation is not mutated).
    """
    existing = _matching_node(ctx, locations)
    if existing is not None:
        return ctx, existing

    from .nodes import Node, NodeSet

    code = "+".join(locations)
    if code in ctx.nodes.codes:
        code = f"{code}_group"
    extra = Node(code=code, label=code, aggregate=True, members=tuple(locations))
    scoped = copy(ctx)
    scoped.nodes = NodeSet(
        nodes=[*ctx.nodes.nodes, extra],
        focus=ctx.nodes.focus,
        aggregate_code=ctx.nodes.aggregate_code,
    )
    return scoped, code


def _indicators_from_member_cache(ctx, locations: Sequence[str], node: str):
    """Build indicators by summing already-extracted member flow tables.

    ``None`` if any member has not been extracted yet — the caller then
    extracts the group as a whole.
    """
    energy_parts = []
    carbon_parts = []
    for loc in locations:
        energy = ctx._files.get(("energy_flows", str(loc)))
        carbon = ctx._files.get(("carbon_flows", str(loc)))
        if energy is None or carbon is None:
            return None
        energy_parts.append(energy)
        carbon_parts.append(carbon)
    energy = _sum_long_tables(energy_parts)
    carbon = _sum_long_tables(carbon_parts)
    if energy.empty:
        return None
    return build(ctx, node, energy=energy, carbon=carbon)


def indicators_for(
    ctx,
    node: str | None = None,
    *,
    members: Sequence[str] | None = None,
) -> Indicators | None:
    """Indicators for one node or an ad-hoc group of real locations.

    Pass **either** ``node`` (a real location, a configured group, or the
    study-wide aggregate) **or** ``members`` (location codes, and/or group
    codes that ``ctx.locations_for`` can expand).  Internal trade between
    members cancels.  No extra library: this is the same graph algebra as
    :func:`for_node`.
    """
    if (node is None) == (members is None):
        raise ValueError("pass exactly one of node= or members=")
    if node is not None:
        return for_node(ctx, node)

    locations = _expand_members(ctx, members)
    if locations is None:
        return None
    scoped, code = _context_for_locations(ctx, locations)
    key = ("indicators", str(code))
    if key in scoped._files:
        return scoped._files[key]
    cached = _indicators_from_member_cache(scoped, locations, code)
    if cached is not None:
        scoped._files[key] = cached
        return cached
    return for_node(scoped, code)


def self_sufficiency(
    ctx,
    node: str | None = None,
    *,
    members: Sequence[str] | None = None,
) -> pd.DataFrame | None:
    """Annual self-sufficiency in percent for primary energy and electricity.

    Returns a DataFrame indexed by year with columns ``primary`` and
    ``electricity``, or ``None`` when flows cannot be extracted.  Values may
    exceed 100 (net exporter) and are not clipped.

    >>> self_sufficiency(ctx, node="BEWAL")
    >>> self_sufficiency(ctx, node="BE")                 # configured group
    >>> self_sufficiency(ctx, members=["BEWAL", "BEVLG", "BEBRU"])
    """
    detail = self_sufficiency_detail(ctx, node=node, members=members)
    return None if detail is None else detail.ratio.copy()


def self_sufficiency_detail(
    ctx,
    node: str | None = None,
    *,
    members: Sequence[str] | None = None,
) -> SelfSufficiency | None:
    """Like :func:`self_sufficiency`, plus the TWh domestic / net-import balances."""
    data = indicators_for(ctx, node=node, members=members)
    if data is None:
        logger.warning("no indicators; self-sufficiency skipped")
        return None
    return data.self_sufficiency
