"""Sankey diagrams of the energy and carbon graphs.

The legacy code had *three* copies of this function --
the legacy ``create_sankey``, the legacy ``create_sankey``
and the legacy ``create_carbon_sankey``.  They differed in five
lines: the value suffix, the node padding, whether the year index was cast to
``int``, which band annotations were drawn, and (in ``sf``'s copy only) whether
the year lookup used ``year`` or ``str(year)``.  Everything else, including two
identical bugs, was duplicated verbatim.

Fixed here:

* ``saf.py:131-136`` and ``:212-217`` assigned ``new_not_empty_nodes`` only
  inside ``if str(year) in nodes_with_links.columns:``.  A horizon whose column
  was missing therefore reused the *previous* iteration's node set -- or raised
  ``NameError`` if it was the first.  The slider is now driven by the frame's
  own index, so the column always exists.
* ``create_sankey`` wrote a ``ColorOpacity`` column into the shared ``NODES``
  frame on every call, mutating the taxonomy for everything downstream
  (``docs/INTERNALS.md`` section 4).  The translucent link colours are computed
  into a local Series.
* The link labels came from ``pd.concat([flows.T, processes], axis=1)`` on two
  indexes that both contained duplicates, so which label a link got depended on
  row order.  Edges are unique by the time they reach here (see
  ``indicators`` FIX 6) and the label map is deduplicated explicitly.
* The value suffix and the slider step labels come from ``section.unit`` and
  from the horizons, not from ``'TWh'`` / ``2019`` / ``2025`` literals.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

import pandas as pd
import plotly.graph_objects as go

from .. import indicators
from .base import CHART_HEIGHT, strip_markup

logger = logging.getLogger(__name__)

#: Flows below this magnitude are not drawn (they clutter the diagram and make
#: plotly's node placement unstable).
MIN_LINK = 1e-4

#: Vertical band captions of the energy Sankey: (x position or node code, text).
ENERGY_BANDS: tuple[tuple[object, str], ...] = (
    (0.0, "Source"),
    ("cms_pe", "Primary energy"),
    (("cms_pe", "elc_fe"), "Secondary energy & networks"),
    ("elc_fe", "Final energy"),
    (1.0, "Demand sector"),
)

#: The carbon Sankey has no meaningful intermediate bands.
CARBON_BANDS: tuple[tuple[object, str], ...] = (
    (0.0, "Source"),
    (1.0, "Final emissions"),
)


def _rgba(color: object, opacity: float = 0.4) -> str | None:
    """``#rrggbb`` -> ``rgba(r,g,b,a)``; anything else is left to plotly."""
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        return None
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{opacity})"


def _link_labels(processes: pd.DataFrame) -> pd.Series:
    """``(Source, Target, Type) -> Label``, one entry per edge."""
    table = processes.dropna(subset=["Source", "Target"])
    keyed = table.set_index(["Source", "Target", "Type"])["Label"]
    return keyed[~keyed.index.duplicated(keep="first")]


def _band_x(positions: pd.Series, spec: object) -> float | None:
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, tuple):
        parts = [positions.get(code) for code in spec]
        if any(p is None or pd.isna(p) for p in parts):
            return None
        return float(sum(parts) / len(parts))
    value = positions.get(spec)
    return None if value is None or pd.isna(value) else float(value)


def sankey(
    ctx,
    flows: pd.DataFrame,
    processes: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
    bands: Sequence[tuple[object, str]] = (),
    pad: int = 8,
) -> go.Figure | None:
    """One Sankey per horizon, behind a slider.

    ``flows`` has ``(Source, Target, Type)`` columns and integer years as its
    index -- the shape :mod:`pypsa2html.indicators` produces.
    """
    if flows is None or flows.empty or not len(flows.columns):
        logger.warning("no flows to draw a Sankey from")
        return None

    nodes = ctx.taxonomy.nodes
    decimals = getattr(ctx.config.project, "decimals", 3)
    values = flows.round(decimals)
    values = values.where(values.abs() >= MIN_LINK, 0.0)

    links = values.T
    links.insert(0, "Label", _link_labels(processes).reindex(links.index).fillna("").to_numpy())
    links = links.reset_index()

    years = [int(y) for y in values.index]
    known = links["Source"].isin(nodes.index) & links["Target"].isin(nodes.index)
    if not known.all():
        unknown = sorted(set(links.loc[~known, "Source"]) | set(links.loc[~known, "Target"]))
        logger.warning("Sankey: dropping edges on node code(s) absent from nodes.csv: %s",
                       [c for c in unknown if c not in nodes.index])
        links = links[known]

    # Per-node totals, taking the larger of the in- and out-flow so a pure sink
    # is treated like a pure source.  Use magnitude so signed carbon links
    # (capture / removal) still mark a node as present.
    magnitude = links.copy()
    for year in years:
        magnitude[year] = links[year].abs()
    outgoing = magnitude.groupby("Source")[years].sum()
    incoming = magnitude.groupby("Target")[years].sum()
    presence = pd.concat([outgoing, incoming]).groupby(level=0).max()
    presence = presence.loc[presence.index.isin(nodes.index)]
    always = presence.index[(presence > 0).all(axis=1)].drop_duplicates()
    # Sorting by x keeps plotly's "snap" arrangement stable across horizons.
    ordered = nodes.reindex(always).dropna(how="all")
    if "PositionX" in ordered.columns:
        ordered = ordered.sort_values(by="PositionX")

    figure = go.Figure()
    steps = []
    for i, year in enumerate(years):
        extra = presence.index[presence[year] > 0].drop_duplicates()
        extra = [code for code in extra if code not in set(ordered.index)]
        year_nodes = pd.concat([ordered, nodes.reindex(extra).dropna(how="all")])
        year_nodes = year_nodes[~year_nodes.index.duplicated(keep="first")]
        if year_nodes.empty:
            continue
        row_index = pd.Series(range(len(year_nodes)), index=year_nodes.index)
        opacity = year_nodes["Color"].map(_rgba)

        # Magnitude for drawing; signed carbon values keep their sign in the
        # label via the raw frame, but plotly needs positive link weights.
        year_links = links.loc[links[year].abs() > 0].copy()
        year_links = year_links[
            year_links["Source"].isin(row_index.index)
            & year_links["Target"].isin(row_index.index)
        ]
        if year_links.empty:
            continue
        node_spec = {
            "label": year_nodes["Label"].tolist(),
            "color": year_nodes["Color"].tolist(),
            "pad": pad,
        }
        if year_nodes["PositionX"].notna().any():
            node_spec["x"] = year_nodes["PositionX"].tolist()
            node_spec["y"] = year_nodes["PositionY"].tolist()

        figure.add_trace(
            go.Sankey(
                valueformat=".0f",
                valuesuffix=" " + strip_markup(unit),
                visible=False,
                arrangement="snap",
                node=node_spec,
                link={
                    "source": row_index.reindex(year_links["Source"]).tolist(),
                    "target": row_index.reindex(year_links["Target"]).tolist(),
                    "color": opacity.reindex(year_links["Source"]).tolist(),
                    "label": year_links["Label"].fillna("").tolist(),
                    "value": year_links[year].abs().tolist(),
                },
            )
        )
        visible = [False] * len(years)
        visible[i] = True
        steps.append(
            {
                "label": str(year),
                "method": "update",
                "args": [{"visible": visible}, {"title": {"text": f"{title} {year}".strip()}}],
            }
        )

    if not figure.data:
        logger.warning("Sankey: no drawable links for any horizon")
        return None

    active = min(len(figure.data) - 1, len(steps) - 1)
    # Rebuild visibility masks to match the traces actually added.
    n_traces = len(figure.data)
    for i, step in enumerate(steps):
        visible = [False] * n_traces
        if i < n_traces:
            visible[i] = True
        step["args"][0]["visible"] = visible
    figure.data[active].visible = True
    figure.update_layout(
        title_text=f"{title} {years[min(active, len(years)-1)]}".strip() or None,
        hovermode="x",
        height=CHART_HEIGHT,
        margin={"l": 20, "r": 20, "t": 90, "b": 20},
        sliders=[{"active": active, "currentvalue": {"visible": False}, "steps": steps}],
    )

    positions = nodes["PositionX"]
    for spec, text in bands:
        x = _band_x(positions, spec)
        if x is not None:
            figure.add_annotation(x=x, y=1.05, text=f"<b>{text}</b>", showarrow=False)
    return figure


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------

def energy(ctx, node: str, section) -> go.Figure | None:
    """The energy-flow Sankey (manifest section ``sankey_energy``)."""
    data = indicators.for_node(ctx, node)
    if data is None:
        return None
    return sankey(
        ctx,
        data.flows,
        ctx.taxonomy.processes_energy,
        unit=section.unit or "TWh/year",
        title=section.title,
        bands=ENERGY_BANDS,
        pad=8,
    )


def carbon(ctx, node: str, section) -> go.Figure | None:
    """The carbon-flow Sankey (manifest section ``sankey_carbon``)."""
    data = indicators.for_node(ctx, node)
    if data is None:
        return None
    return sankey(
        ctx,
        data.flows_co2,
        ctx.taxonomy.processes_carbon,
        unit=section.unit or "MtCO2",
        title=section.title,
        bands=CARBON_BANDS,
        pad=25,
    )
