"""Multi-scenario overview chart builders for the landing page.

Each builder reads **all** configured scenarios via :func:`scenario_contexts`,
skipping any whose ``results_dir`` is missing so a comparison page still
renders when only some scenarios have been solved.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from .. import indicators
from ..context import BuildContext, build_context
from ..extract.tables import capacity_table, cost_table
from .base import (
    CHART_HEIGHT,
    FONT_SIZE,
    add_chart_data,
    area_chart,
    combine_charts,
    ghg_area_chart,
    stacked_bar,
    strip_markup,
    tech_color_map,
)

logger = logging.getLogger(__name__)

#: MW -> GW / MWh -> GWh for capacity charts.
_CAPACITY_SCALE = 1e-3


def scenario_contexts(ctx) -> dict[str, BuildContext]:
    """Build/cache a :class:`BuildContext` per configured scenario.

    Returns a mapping of scenario *label* -> context.  Scenarios whose
    ``results_dir`` is absent are skipped with a warning.

    When the site build has installed a shared context pool on ``ctx``
    (see :func:`pypsa2html.build.build_site`), contexts are reused across
    scenarios so overview pages do not re-extract every network three times.
    """
    key = ("scenario_contexts",)
    cached = ctx._files.get(key)
    if cached is not None:
        return cached

    config = getattr(ctx, "config", None)
    scenario_list = getattr(config, "scenarios", None) if config is not None else None
    if not scenario_list:
        logger.warning("no scenarios configured; scenario comparison skipped")
        ctx._files[key] = {}
        return {}

    pool = ctx._files.get(("_context_pool",))

    contexts: dict[str, BuildContext] = {}
    for sc in scenario_list:
        results_dir = config.results_dir(sc.name)
        if not results_dir.exists():
            logger.warning(
                "scenario %s: results_dir missing, skipping: %s",
                sc.label,
                results_dir,
            )
            continue
        try:
            if isinstance(pool, dict):
                if sc.name not in pool:
                    other = build_context(config, sc.name)
                    other._files[("_context_pool",)] = pool
                    pool[sc.name] = other
                contexts[sc.label] = pool[sc.name]
            else:
                contexts[sc.label] = build_context(config, sc.name)
        except FileNotFoundError as exc:
            logger.warning("scenario %s: %s", sc.label, exc)
            continue

    ctx._files[key] = contexts
    return contexts


def _scenario_palette(labels: list[str]) -> dict[str, str]:
    """Distinct colours for scenario series (copied defaults, never mutated)."""
    defaults = [
        "#01889f",
        "#11875d",
        "#c14a09",
        "#9a0200",
        "#fcc006",
        "#95d0fc",
        "#fd5956",
    ]
    return {label: defaults[i % len(defaults)] for i, label in enumerate(labels)}


def _grouped_bar(
    df: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
    scale: float = 1.0,
) -> go.Figure | None:
    """Grouped bar chart: index = years, columns = scenario labels.

    Scenarios are *alternatives*, not parts of a whole, so their bars sit side
    by side: stacking them draws a "total" that is the sum of mutually
    exclusive futures.  Unlike :func:`~pypsa2html.charts.base.stacked_bar`
    there is no positive/negative trace split either -- a grouped bar already
    grows downwards when its value is negative, and splitting the signs would
    give each of them its own slot in the group.
    """
    if df is None or df.empty or not len(df.columns):
        return None
    data = df.astype(float) * scale
    years = [str(y) for y in data.index]
    palette = _scenario_palette(list(data.columns))
    fig = go.Figure()
    for label in data.columns:
        fig.add_trace(
            go.Bar(
                x=years,
                y=data[label],
                name=str(label),
                marker_color=palette.get(str(label), "lightgrey"),
                hovertemplate="%{y:.3g}",
            )
        )
    fig.update_layout(
        title=title or None,
        barmode="group",
        height=CHART_HEIGHT,
        hovermode="x unified",
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        legend_title_text="",
        margin={"l": 60, "r": 30, "t": 60 if title else 30, "b": 50},
        xaxis={"tickmode": "array", "tickvals": years, "title": ""},
    )
    return fig


def _scenario_line_chart(
    df: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
) -> go.Figure | None:
    """Line chart with scenario labels as series (no taxonomy relabelling)."""
    if df is None or df.empty or not len(df.columns):
        return None
    palette = _scenario_palette(list(df.columns))
    fig = go.Figure()
    for label in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df[label],
                name=str(label),
                mode="lines+markers",
                line={"width": 2, "color": palette.get(str(label))},
                connectgaps=False,
                hovertemplate="%{y:.1f}",
            )
        )
    years = [str(y) for y in df.index]
    fig.update_layout(
        title=title or None,
        hovermode="x",
        height=CHART_HEIGHT,
        legend_title_text="",
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        margin={"l": 60, "r": 30, "t": 60 if title else 30, "b": 50},
        xaxis={"tickmode": "array", "tickvals": years, "title": ""},
    )
    return fig


def _table_comparison(
    ctx,
    node: str,
    section,
    *,
    table_fn,
    signed: bool = True,
    scale: float = 1.0,
) -> go.Figure | None:
    """Compare scenarios via total grouped bars and per-scenario stacked tech views."""
    scenarios = scenario_contexts(ctx)
    if not scenarios:
        logger.warning("section %s: no scenario contexts for node %s", section.id, node)
        return None

    totals: dict[str, pd.Series] = {}
    stacks: dict[str, pd.DataFrame] = {}
    for label, sctx in scenarios.items():
        table = table_fn(sctx, node)
        if table is None or table.empty:
            logger.warning(
                "section %s: no data for scenario %s, node %s",
                section.id,
                label,
                node,
            )
            continue
        years = [y for y in sctx.year_columns if y in table.columns]
        if not years:
            years = [str(c) for c in table.columns if str(c).isdigit()]
        if not years:
            continue
        sub = table.reindex(columns=years, fill_value=0.0)
        totals[label] = sub.sum(axis=0)
        stacks[label] = sub

    if not totals:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None

    total_df = pd.DataFrame(totals)
    total_df.index = [str(y) for y in total_df.index]
    add_chart_data(ctx, node, section.title, section.unit, total_df.T)

    figures: list[tuple[str, go.Figure | None]] = [
        ("Total", _grouped_bar(total_df, unit=section.unit, scale=scale)),
    ]
    colors = tech_color_map()
    for label, table in stacks.items():
        figures.append(
            (
                label,
                stacked_bar(
                    table,
                    colors,
                    unit=section.unit,
                    title="",
                    signed=signed,
                    scale=scale,
                ),
            )
        )

    return combine_charts(figures, menu_title="View")


def cumulative_emissions(ctx, node: str, section) -> go.Figure | None:
    scenarios = scenario_contexts(ctx)
    if not scenarios:
        logger.warning("section %s: no scenario contexts for node %s", section.id, node)
        return None

    figures: list[tuple[str, go.Figure | None]] = []
    for label, sctx in scenarios.items():
        data = indicators.for_node(sctx, node)
        if data is None:
            logger.warning(
                "section %s: no indicators for scenario %s, node %s",
                section.id,
                label,
                node,
            )
            continue
        frame = data.ghg_sector_cum
        if frame is None or frame.empty or not len(frame.columns):
            continue
        add_chart_data(ctx, node, f"{section.title} - {label}", section.unit, frame)
        figures.append(
            (label, ghg_area_chart(sctx, frame, unit=section.unit)),
        )

    if not figures:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    return combine_charts(figures, menu_title="Scenario")


def energy_comparison(ctx, node: str, section) -> go.Figure | None:
    scenarios = scenario_contexts(ctx)
    if not scenarios:
        logger.warning("section %s: no scenario contexts for node %s", section.id, node)
        return None

    totals: dict[str, pd.Series] = {}
    breakdowns: dict[str, pd.DataFrame] = {}
    for label, sctx in scenarios.items():
        data = indicators.for_node(sctx, node)
        if data is None:
            logger.warning(
                "section %s: no indicators for scenario %s, node %s",
                section.id,
                label,
                node,
            )
            continue
        fec = data.fec_carrier
        if fec is None or fec.empty:
            continue
        totals[label] = fec.sum(axis=1)
        breakdowns[label] = fec

    if not totals:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None

    total_df = pd.DataFrame(totals)
    add_chart_data(ctx, node, section.title, section.unit, total_df.T)

    figures: list[tuple[str, go.Figure | None]] = [
        ("Total", _scenario_line_chart(total_df, unit=section.unit)),
    ]
    for label, fec in breakdowns.items():
        sctx = scenarios[label]
        figures.append((label, area_chart(sctx, fec, unit=section.unit)))

    return combine_charts(figures, menu_title="Scenario")


def costs(ctx, node: str, section) -> go.Figure | None:
    return _table_comparison(
        ctx,
        node,
        section,
        table_fn=lambda sctx, n: cost_table(sctx, n, "total"),
        signed=True,
    )


def investment_costs(ctx, node: str, section) -> go.Figure | None:
    return _table_comparison(
        ctx,
        node,
        section,
        table_fn=lambda sctx, n: cost_table(sctx, n, "capital"),
        signed=True,
    )


def operational_costs(ctx, node: str, section) -> go.Figure | None:
    return _table_comparison(
        ctx,
        node,
        section,
        table_fn=lambda sctx, n: cost_table(sctx, n, "marginal"),
        signed=True,
    )


def capacities(ctx, node: str, section) -> go.Figure | None:
    return _table_comparison(
        ctx,
        node,
        section,
        table_fn=lambda sctx, n: capacity_table(sctx, n, "power"),
        signed=False,
        scale=_CAPACITY_SCALE,
    )


def storage_capacities(ctx, node: str, section) -> go.Figure | None:
    return _table_comparison(
        ctx,
        node,
        section,
        table_fn=lambda sctx, n: capacity_table(sctx, n, "storage"),
        signed=False,
        scale=_CAPACITY_SCALE,
    )


def cc_capacities(ctx, node: str, section) -> go.Figure | None:
    """Carbon-capture capacity comparison — not yet ported."""
    logger.debug("section %s: cc_capacities not implemented", section.id)
    return None


def energy_independence(ctx, node: str, section) -> go.Figure | None:
    """Self-sufficiency across scenarios, for primary energy and electricity.

    Replaces the legacy mean-of-clipped-carrier-coverage metric, which mixed
    geographic self-sufficiency with the renewable share of gas and oil and
    hid net exporters.  Groups and the study-wide aggregate are included —
    internal trade cancels, so ``BE`` is Belgium, not the sum of regional
    ratios.
    """
    scenarios = scenario_contexts(ctx)
    if not scenarios:
        logger.warning("section %s: no scenario contexts for node %s", section.id, node)
        return None

    figures = []
    for kind, code in indicators.SELF_SUFFICIENCY_KINDS:
        label = str(ctx.taxonomy.label(code))
        series: dict[str, pd.Series] = {}
        for scen_label, sctx in scenarios.items():
            data = indicators.for_node(sctx, node)
            if data is None:
                logger.warning(
                    "section %s: no indicators for scenario %s, node %s",
                    section.id,
                    scen_label,
                    node,
                )
                continue
            series[scen_label] = data.self_sufficiency.ratio[kind]
        if not series:
            continue
        frame = pd.DataFrame(series)
        add_chart_data(ctx, node, f"{section.title} - {label}", section.unit, frame.T)
        fig = _scenario_line_chart(frame, unit=section.unit)
        if fig is not None:
            fig.add_hline(
                y=100,
                line_dash="dash",
                line_color="#888888",
                annotation_text="self-sufficient",
                annotation_position="top left",
            )
        figures.append((label, fig))

    if not figures:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    return combine_charts(figures)
