"""Network costs: what a region pays for its grids, by layer and by neighbour.

Two sources, read side by side because they answer different questions:

* **The calibrated long table** a project may write next to its results
  (``features.network_costs.file``, pypsa-wal's
  ``csvs/network_costs_calibrated.csv``).  It is the only place where the
  *existing* grids are valued at their regulated cost rather than at the
  model's annuity, and where the add-ons a tariff carries (public-service
  obligations, road-use fee, smart meters) and the grid losses sit.  Its rows
  follow a four-layer convention:

  ``L1``  existing grid, capital charges (sunk, regulated accounts)
  ``L2``  increment: capacity the model builds after the first horizon
  ``L3``  non-capacity operating costs
  ``L4``  losses and regulated add-ons
  ``memo`` shown for reference, never added

  The table has no node column: it describes one region, named in
  ``features.network_costs.node``.  Every other node gets no tariff view.

* **The solved networks**, for the branches that leave a node: every AC line
  and every DC / gas / H2 / CO2 pipeline link with exactly one end in the
  node, valued at model cost and split 50/50 between its two ends (the
  convention of the project's nodal cost tables), with the capacity the first
  horizon already had kept apart from what the model added.  That is the
  view "interconnections with the neighbouring nodes", by neighbour.  For a
  group node (``BE``) a branch with both ends inside the group is an
  *internal* branch and counts in full.

A one-bus region has no modelled internal grid, so its *internal* transmission
cost only exists in the tariff view.  The charts say so rather than draw an
empty bar that reads as "free".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..context import BuildContext

logger = logging.getLogger(__name__)

#: Default location of the calibrated long table, under the results tree.
DEFAULT_FILE = "csvs/network_costs_calibrated.csv"

#: Default location of the project's cost-by-segment totals (optional).
DEFAULT_SEGMENTS_FILE = "csvs/network_cost_segments.csv"

#: Columns the calibrated long table must carry.
REQUIRED_COLUMNS = ("network", "view", "layer", "item", "year", "value")

#: Branch carriers and the network they belong to.  Reversed twins (the
#: ``-reversed`` copies PyPSA-Eur adds so a link can flow both ways) are the
#: same asset and are dropped before anything is summed.
BRANCH_GROUPS: dict[str, str] = {
    "AC": "electricity",
    "DC": "electricity",
    "gas pipeline": "methane",
    "gas pipeline new": "methane",
    "H2 pipeline": "hydrogen",
    "H2 pipeline retrofitted": "hydrogen",
    "H2 pipeline (Kernnetz)": "hydrogen",
    "CO2 pipeline": "CO2",
}

#: Display order of the branch networks.
BRANCH_ORDER: tuple[str, ...] = ("electricity", "methane", "hydrogen", "CO2")

#: The label of the "both ends inside the node" bucket of a group node.
INTERNAL = "internal"


@dataclass(frozen=True)
class NetworkCostsSpec:
    """Resolved ``features.network_costs`` block."""

    file: str
    node: str | None
    references: dict
    segments_file: str | None = DEFAULT_SEGMENTS_FILE

    @classmethod
    def from_config(cls, ctx) -> NetworkCostsSpec:
        raw = getattr(getattr(ctx.config, "features", None), "network_costs", None) or {}
        return cls(
            file=str(raw.get("file") or DEFAULT_FILE),
            node=raw.get("node"),
            references=dict(raw.get("references") or {}),
            segments_file=raw.get("segments_file", DEFAULT_SEGMENTS_FILE),
        )


# ---------------------------------------------------------------------------
# Calibrated long table
# ---------------------------------------------------------------------------

def calibrated_table(ctx: BuildContext, node: str) -> pd.DataFrame | None:
    """The calibrated long table, if it exists *and* describes ``node``.

    Returns ``None`` for every other node, and when the file is absent, so the
    builders fall back to the network view alone.
    """
    spec = NetworkCostsSpec.from_config(ctx)
    if spec.node is None:
        return None
    if str(node) != str(spec.node):
        return None
    raw = ctx.read_csv(spec.file, base="results")
    if raw is None:
        return None
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        logger.warning("%s: missing column(s) %s, network-cost view skipped", spec.file, missing)
        return None
    out = raw.copy()
    out["year"] = out["year"].astype(int).astype(str)
    out["value"] = pd.to_numeric(out["value"], errors="coerce").fillna(0.0)
    return out


def wide(table: pd.DataFrame, years: list[str]) -> pd.DataFrame:
    """Pivot the long table to (network, view, layer, item) x year, M€/a."""
    pivot = table.pivot_table(
        index=["network", "view", "layer", "item"],
        columns="year",
        values="value",
        aggfunc="sum",
        sort=False,
    )
    return pivot.reindex(columns=years).fillna(0.0)


# ---------------------------------------------------------------------------
# Branches from the networks
# ---------------------------------------------------------------------------

def _is_twin(links: pd.DataFrame) -> pd.Series:
    twin = pd.Series(links.index.str.endswith("-reversed"), index=links.index)
    if "reversed" in links.columns:
        twin |= links["reversed"].fillna(False).astype(bool)
    return twin


def _branches(n) -> pd.DataFrame:
    """One row per transmission asset: carrier, end locations, nominal, cost."""
    loc = n.buses["location"] if "location" in n.buses.columns else pd.Series(dtype=object)
    frames = []
    lines = n.lines
    if len(lines):
        frames.append(pd.DataFrame({
            "carrier": "AC",
            "loc0": lines.bus0.map(loc).fillna(lines.bus0),
            "loc1": lines.bus1.map(loc).fillna(lines.bus1),
            "nom": lines.s_nom.astype(float),
            "nom_opt": lines.s_nom_opt.astype(float),
            "capital_cost": lines.capital_cost.astype(float),
            # Lines are never re-vintaged in the myopic chain: one asset, grown.
            "build_year": -1,
        }, index=lines.index))
    links = n.links
    keep = links.carrier.isin(BRANCH_GROUPS) & ~_is_twin(links)
    links = links.loc[keep]
    if len(links):
        frames.append(pd.DataFrame({
            "carrier": links.carrier,
            "loc0": links.bus0.map(loc).fillna(links.bus0),
            "loc1": links.bus1.map(loc).fillna(links.bus1),
            "nom": links.p_nom.astype(float),
            "nom_opt": links.p_nom_opt.astype(float),
            "capital_cost": links.capital_cost.astype(float),
            "build_year": links.build_year.astype(int) if "build_year" in links else 0,
        }, index=links.index))
    if not frames:
        return pd.DataFrame(
            columns=["carrier", "loc0", "loc1", "nom", "nom_opt", "capital_cost", "build_year", "network"]
        )
    out = pd.concat(frames)
    out["network"] = out["carrier"].map(BRANCH_GROUPS)
    return out


def _first_horizon_nominal(ctx: BuildContext) -> pd.Series:
    """Branch capacity in the first solved horizon, before its own expansion.

    ``s_nom`` / ``p_nom`` of the first network is what existed when the
    pathway started; anything above it, in any later horizon, is new.
    """
    key = ("network_costs", "first_nominal")
    if key in ctx._files:
        return ctx._files[key]
    first = min(ctx.horizons)
    nominal = _branches(ctx.networks[first])["nom"]
    ctx._files[key] = nominal
    return nominal


def branch_costs(ctx: BuildContext, node: str) -> pd.DataFrame | None:
    """Model cost of the branches leaving (or inside) ``node``, M€/a.

    Long table: ``network`` (electricity / methane / hydrogen / CO2),
    ``counterpart`` (the location at the other end, or :data:`INTERNAL`),
    ``status`` (``existing`` / ``new``), ``year``, ``value``.

    * A branch with one end in the node counts half (50/50 convention).
    * A branch with both ends in the node counts in full and is
      :data:`INTERNAL` -- only possible for a group node.
    * ``existing`` is the part of the optimised capacity the first horizon
      already had (``min(nom_opt, nom at the first horizon)``, zero for a
      vintage built later); ``new`` is the rest.  Both at the horizon's
      annuity, which for ``existing`` is a model valuation of a sunk asset.

    ``None`` for the study-wide node, where every branch is internal and the
    split by neighbour says nothing.
    """
    if getattr(ctx, "networks", None) is None or not getattr(ctx, "horizons", None):
        return None
    locations = ctx.locations_for(node)
    if locations is None:
        return None
    inside = {str(c) for c in locations}
    first = min(ctx.horizons)
    nominal0 = _first_horizon_nominal(ctx)
    rows = []
    for horizon in ctx.horizons:
        try:
            n = ctx.networks[horizon]
        except FileNotFoundError:
            continue
        br = _branches(n)
        if br.empty:
            continue
        in0 = br.loc0.astype(str).isin(inside)
        in1 = br.loc1.astype(str).isin(inside)
        touch = in0 | in1
        br = br.loc[touch]
        if br.empty:
            continue
        in0, in1 = in0[touch], in1[touch]
        weight = np.where(in0 & in1, 1.0, 0.5)
        counterpart = np.where(in0 & in1, INTERNAL, np.where(in0, br.loc1, br.loc0))
        nom0 = nominal0.reindex(br.index).fillna(0.0)
        existing = np.minimum(br.nom_opt, nom0).where(br.build_year <= first, 0.0)
        new = (br.nom_opt - existing).clip(lower=0.0)
        frame = pd.DataFrame({
            "network": br.network.to_numpy(),
            "counterpart": counterpart.astype(str),
            "existing": weight * existing.to_numpy() * br.capital_cost.to_numpy() / 1e6,
            "new": weight * new.to_numpy() * br.capital_cost.to_numpy() / 1e6,
        })
        frame = frame.melt(
            id_vars=["network", "counterpart"], var_name="status", value_name="value"
        )
        frame = frame.groupby(["network", "counterpart", "status"], as_index=False).value.sum()
        frame["year"] = str(horizon)
        rows.append(frame)
    if not rows:
        return None
    out = pd.concat(rows, ignore_index=True)
    return out.loc[out.value.abs() > 1e-9].reset_index(drop=True)


def branch_wide(table: pd.DataFrame, years: list[str]) -> pd.DataFrame:
    """Pivot :func:`branch_costs` to (network, counterpart, status) x year."""
    if table is None or table.empty:
        return pd.DataFrame(columns=years)
    pivot = table.pivot_table(
        index=["network", "counterpart", "status"],
        columns="year",
        values="value",
        aggfunc="sum",
    )
    return pivot.reindex(columns=years).fillna(0.0)
