"""Cost, capacity and demand chart builders named in ``data/pages.yaml``.

Each builder has the ``(ctx, node, section) -> Figure | None`` signature from
``docs/INTERNALS.md`` §4.  Data comes from :mod:`pypsa2html.extract.tables`.

Capacity pages expose both a full stacked bar (all technologies) and the
legacy faceted panels (curated technology groups).
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..extract.tables import capacity_table, cost_table, demand_table
from .base import (
    FONT_SIZE,
    add_chart_data,
    resolve_tech_color,
    stacked_bar,
    tech_color_map,
)

logger = logging.getLogger(__name__)

#: MW -> GW / MWh -> GWh for capacity charts.
_CAPACITY_SCALE = 1e-3

#: Faceted capacity panels — port of ``create_capacity_chart`` groups.
_POWER_GROUPS: list[list[str]] = [
    ["solar"],
    ["onshore wind", "offshore wind"],
    ["power-to-heat"],
    ["power-to-gas"],
    ["transmission lines"],
    ["nuclear"],
    ["CCGT"],
    ["CHP"],
]

#: Aggregate-node panels swap CHP for power-to-liquid (legacy ``groupss``).
_POWER_GROUPS_AGGREGATE: list[list[str]] = [
    ["solar"],
    ["onshore wind", "offshore wind"],
    ["power-to-heat"],
    ["power-to-gas"],
    ["transmission lines"],
    ["power-to-liquid"],
    ["CCGT"],
    ["nuclear"],
]

_STORAGE_GROUPS: list[list[str]] = [
    ["Grid-scale battery"],
    ["Thermal Energy Storage"],
    ["Gas storage"],
]


def _bar(ctx, node, section, table, *, signed=True, scale=1.0) -> go.Figure | None:
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return stacked_bar(
        table,
        tech_color_map(),
        unit=section.unit,
        title="",
        signed=signed,
        scale=scale,
    )


def annual_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "total"), signed=True)


def clustered_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "clustered"), signed=False)


def investment_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "capital"), signed=True)


def operational_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "marginal"), signed=True)


def _smart_capitalize(phrase: str) -> str:
    if not phrase or phrase[0].isupper():
        return phrase
    return phrase[0].upper() + phrase[1:]


def _panel_title(techs: list[str]) -> str:
    return ", ".join(_smart_capitalize(t) for t in techs)


def _faceted_capacity_chart(
    table: pd.DataFrame,
    groups: list[list[str]],
    *,
    unit: str,
    scale: float = _CAPACITY_SCALE,
    shared_y: bool = True,
    height: int = 800,
) -> go.Figure | None:
    """Legacy-style capacity panels: one subplot per technology group.

    Missing technologies still get a zero-height bar so the panel (e.g.
    Nuclear) is never a blank hole when the group is configured.
    """
    if table is None or table.empty:
        return None

    data = table.astype(float) * scale
    years = [str(c) for c in data.columns]
    n_groups = len(groups)
    if n_groups == 0:
        return None

    # Prefer a 2×N grid when there are several panels; storage uses 1×N.
    if n_groups <= 3:
        rows, cols = 1, n_groups
    else:
        cols = (n_groups + 1) // 2
        rows = 2

    titles = [_panel_title(g) for g in groups]
    fig = make_subplots(
        rows=rows,
        cols=cols,
        subplot_titles=titles,
        shared_yaxes=shared_y,
    )
    palette = tech_color_map()
    any_trace = False

    for i, tech_group in enumerate(groups):
        if rows == 1:
            row_idx, col_idx = 1, i + 1
        else:
            row_idx = 1 if i < cols else 2
            col_idx = i + 1 if i < cols else i - cols + 1

        for tech in tech_group:
            if tech in data.index:
                series = data.loc[tech, years]
            else:
                series = pd.Series(0.0, index=years)
            fig.add_trace(
                go.Bar(
                    x=years,
                    y=series.values,
                    name=_smart_capitalize(tech),
                    marker_color=resolve_tech_color(tech, palette),
                    hovertemplate="%{y:.3g}",
                ),
                row=row_idx,
                col=col_idx,
            )
            any_trace = True

    if not any_trace:
        return None

    fig.update_layout(
        height=height,
        showlegend=True,
        font={"size": FONT_SIZE},
        legend={"font": {"size": FONT_SIZE}},
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        barmode="group",
    )
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            fig.update_xaxes(tickfont={"size": 13}, row=r, col=c)
            fig.update_yaxes(tickfont={"size": 13}, row=r, col=c)
    fig.update_yaxes(title_text=unit, row=rows, col=1, title_font={"size": 15})
    return fig


def capacities(ctx, node: str, section) -> go.Figure | None:
    """Stacked bar of all power capacities (full technology list)."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "power"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def capacities_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Faceted panels for the curated technology groups (legacy layout)."""
    table = capacity_table(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    groups = (
        _POWER_GROUPS_AGGREGATE
        if hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node)
        else _POWER_GROUPS
    )
    return _faceted_capacity_chart(table, groups, unit=section.unit)


def storage_capacities(ctx, node: str, section) -> go.Figure | None:
    """Stacked bar of all storage capacities."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "storage"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def storage_capacities_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Faceted panels for battery / TES / gas storage (legacy layout)."""
    table = capacity_table(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return _faceted_capacity_chart(
        table,
        _STORAGE_GROUPS,
        unit=section.unit,
        shared_y=False,
        height=500,
    )


def sectoral_demands(ctx, node: str, section) -> go.Figure | None:
    table = demand_table(ctx, node)
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    groups = table.attrs.get("groups")
    if groups is None or groups.empty:
        return stacked_bar(
            table, tech_color_map(), unit=section.unit, title="", signed=False
        )

    figures: list[tuple[str, go.Figure]] = []
    colors = tech_color_map()
    for group in sorted(set(groups.dropna())):
        sectors = groups.index[groups == group]
        sub = table.loc[table.index.intersection(sectors)]
        if sub.empty:
            continue
        fig = stacked_bar(sub, colors, unit=section.unit, title=group, signed=False)
        if fig is not None:
            figures.append((group, fig))
    if not figures:
        return stacked_bar(
            table, colors, unit=section.unit, title="", signed=False
        )
    if len(figures) == 1:
        return figures[0][1]

    from .base import combine_charts

    return combine_charts(figures, menu_title="Demand")
