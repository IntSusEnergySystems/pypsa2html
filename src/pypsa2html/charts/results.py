"""Cost, capacity and demand chart builders named in ``data/pages.yaml``.

Each builder has the ``(ctx, node, section) -> Figure | None`` signature from
``docs/INTERNALS.md`` §4.  Data comes from :mod:`pypsa2html.extract.tables`;
rendering goes through the shared :func:`~pypsa2html.charts.base.stacked_bar`.
"""

from __future__ import annotations

import logging

import plotly.graph_objects as go

from ..extract.tables import capacity_table, cost_table, demand_table
from .base import add_chart_data, stacked_bar, tech_color_map

logger = logging.getLogger(__name__)

#: MW -> GW / MWh -> GWh for capacity charts.
_CAPACITY_SCALE = 1e-3


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


def capacities(ctx, node: str, section) -> go.Figure | None:
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "power"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def storage_capacities(ctx, node: str, section) -> go.Figure | None:
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "storage"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def sectoral_demands(ctx, node: str, section) -> go.Figure | None:
    table = demand_table(ctx, node)
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    # Facet by demand group when the extractor attached group labels.
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
