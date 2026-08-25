"""Indicator chart builders named in ``data/pages.yaml``.

Each function has the signature ``docs/INTERNALS.md`` section 4 defines --
``(ctx, node, section) -> Figure | Html | None`` -- and each one is reached
only when the manifest lists its section.  Section identity is the manifest's
business: the unique ``id`` is the anchor, and the page it sits on is where it
renders.
"""

from __future__ import annotations

import logging

import plotly.graph_objects as go

from .. import indicators
from .base import (
    add_chart_data,
    area_chart,
    combine_charts,
    ghg_area_chart,
    line_chart,
    stacked_bar,
)

logger = logging.getLogger(__name__)


def _data(ctx, node: str, section):
    """The indicator set, or ``None`` with a warning naming the section."""
    result = indicators.for_node(ctx, node)
    if result is None:
        logger.warning("section %s: no indicators for node %s", section.id, node)
    return result


def _single(ctx, node, section, frame, builder) -> go.Figure | None:
    """Record the numbers and build one figure, or return ``None``."""
    if frame is None or frame.empty or not len(frame.columns):
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, frame)
    return builder(ctx, frame, unit=section.unit)


def _variants(ctx, node, section, variants) -> go.Figure | None:
    """Record and build one figure per variant, behind a dropdown."""
    figures = []
    for label, frame in variants:
        if frame is None or frame.empty or not len(frame.columns):
            continue
        add_chart_data(ctx, node, f"{section.title} - {label}", section.unit, frame)
        figures.append((label, area_chart(ctx, frame, unit=section.unit)))
    if not figures:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    return combine_charts(figures)


# ---------------------------------------------------------------------------
# Emissions
# ---------------------------------------------------------------------------


def ghg_by_sector(ctx, node: str, section) -> go.Figure | None:
    data = _data(ctx, node, section)
    return None if data is None else _single(ctx, node, section, data.ghg_sector, ghg_area_chart)


def ghg_by_source(ctx, node: str, section) -> go.Figure | None:
    data = _data(ctx, node, section)
    return None if data is None else _single(ctx, node, section, data.ghg_source, ghg_area_chart)


def ghg_cumulative_by_sector(ctx, node: str, section) -> go.Figure | None:
    data = _data(ctx, node, section)
    return (
        None if data is None else _single(ctx, node, section, data.ghg_sector_cum, ghg_area_chart)
    )


def ghg_cumulative_by_source(ctx, node: str, section) -> go.Figure | None:
    data = _data(ctx, node, section)
    return (
        None if data is None else _single(ctx, node, section, data.ghg_source_cum, ghg_area_chart)
    )


# ---------------------------------------------------------------------------
# Energy consumption
# ---------------------------------------------------------------------------


def renewable_share(ctx, node: str, section) -> go.Figure | None:
    """Renewable share of each final carrier, and of the system as a whole."""
    data = _data(ctx, node, section)
    return None if data is None else _single(ctx, node, section, data.ren_cov_ratio, line_chart)


def fec_by_origin(ctx, node: str, section) -> go.Figure | None:
    """Primary supply split into renewable, fossil and nuclear."""
    data = _data(ctx, node, section)
    return None if data is None else _single(ctx, node, section, data.gfec_breakdown, area_chart)


def domestic_share(ctx, node: str, section) -> go.Figure | None:
    """Share of each carrier's consumption covered from domestic supply."""
    data = _data(ctx, node, section)
    return None if data is None else _single(ctx, node, section, data.cov_ratio, line_chart)


def _ratio_frame(detail):
    """Map ``primary`` / ``electricity`` onto taxonomy codes for colours."""
    rename = {kind: code for kind, code in indicators.SELF_SUFFICIENCY_KINDS}
    return detail.ratio.rename(columns=rename)


def _add_autarky_line(fig: go.Figure | None) -> go.Figure | None:
    if fig is None:
        return None
    fig.add_hline(
        y=100,
        line_dash="dash",
        line_color="#888888",
        annotation_text="self-sufficient",
        annotation_position="top left",
    )
    return fig


def self_sufficiency(ctx, node: str, section) -> go.Figure | None:
    """Primary-energy and electricity self-sufficiency, unclipped percent."""
    data = _data(ctx, node, section)
    if data is None:
        return None
    frame = _ratio_frame(data.self_sufficiency)
    fig = _single(ctx, node, section, frame, line_chart)
    return _add_autarky_line(fig)


def self_sufficiency_balance(ctx, node: str, section) -> go.Figure | None:
    """Domestic supply versus net imports, for primary energy and electricity."""
    data = _data(ctx, node, section)
    if data is None:
        return None
    detail = data.self_sufficiency
    colors = {
        "Domestic": str(ctx.taxonomy.colors.get("prod", "#32bf84")),
        "Net imports": str(ctx.taxonomy.colors.get("imp", "#ffb07c")),
    }
    figures = []
    for label, frame in (
        ("Primary energy", detail.primary),
        ("Electricity", detail.electricity),
    ):
        if frame is None or frame.empty:
            continue
        named = frame.rename(columns={"domestic": "Domestic", "net_import": "Net imports"})
        add_chart_data(ctx, node, f"{section.title} - {label}", section.unit, named)
        figures.append((label, stacked_bar(named.T, colors, section.unit, signed=True)))
    if not figures:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    return combine_charts(figures)


def fec_by_sector(ctx, node: str, section) -> go.Figure | None:
    """All sectors together, then one variant per final carrier."""
    data = _data(ctx, node, section)
    if data is None:
        return None
    label = ctx.taxonomy.label
    variants = [("All energies", data.fec_sector)]
    variants += [(label(code), frame) for code, frame in data.fec_by_sector.items()]
    return _variants(ctx, node, section, variants)


def secondary_mix(ctx, node: str, section) -> go.Figure | None:
    """One variant per secondary carrier: what produces it."""
    data = _data(ctx, node, section)
    if data is None:
        return None
    label = ctx.taxonomy.label
    variants = [(label(code), frame) for code, frame in data.sec_mix.items()]
    return _variants(ctx, node, section, variants)


def fec_by_carrier(ctx, node: str, section) -> go.Figure | None:
    """All carriers together, then one variant per demand sector."""
    data = _data(ctx, node, section)
    if data is None:
        return None
    label = ctx.taxonomy.label
    variants = [("All sectors", data.fec_carrier)]
    variants += [(label(code), frame) for code, frame in data.fec_by_carrier.items()]
    return _variants(ctx, node, section, variants)
