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

from ..carriers import ccs_parent_carrier
from ..extract.tables import (
    capacity_table,
    cost_table,
    demand_table,
    utilisation_table,
)
from .base import (
    FONT_SIZE,
    add_chart_data,
    grouped_bar,
    resolve_tech_color,
    stacked_bar,
    tech_color_map,
)

logger = logging.getLogger(__name__)


def _study_wide_panels(ctx, node: str) -> bool:
    """Faceted CHP/PtL swap is for the study-wide sum, not a group aggregate."""
    if hasattr(ctx, "is_study_wide"):
        return ctx.is_study_wide(node)
    return hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node)


#: MW -> GW / MWh -> GWh for capacity charts.
_CAPACITY_SCALE = 1e-3

#: The three power-to-fuel conversion routes share one panel: they compete for
#: the same electricity and each other's output (H2 feeds Fischer-Tropsch and
#: methanation), so the interesting comparison is between them, not between
#: each one and a blank neighbour.  They used to occupy three separate panels.
_POWER_TO_FUEL: list[str] = ["electrolysis", "methanation", "Fischer-Tropsch"]

#: Faceted capacity panels — port of ``create_capacity_chart`` groups.
_POWER_GROUPS: list[list[str]] = [
    ["solar"],
    ["onshore wind", "offshore wind"],
    ["power-to-heat"],
    _POWER_TO_FUEL,
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
    _POWER_TO_FUEL,
    ["transmission lines"],
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


#: Panels whose capacity is not a plain electrical rating.
#:
#: Every power plant is restated to **GW_e** by
#: :func:`pypsa2html.extract.capacity_filter.electric_output_scaling`, so CCGT,
#: CHP, OCGT and nuclear share an axis with wind and solar Generators.  Two
#: groups keep another convention and say so in their title:
#:
#: * ``power-to-heat`` is restated on the *heat* side by
#:   :func:`pypsa2html.extract.capacity_filter.heat_output_scaling` — heat-pump
#:   ``p_nom`` is already MW_th and resistive heaters are scaled by their
#:   efficiency — so the panel reads GW_th;
#: * the power-to-fuel routes are rated at their **input**: electrolysis in
#:   GW_e of electricity drawn, Fischer-Tropsch and methanation in GW of H2
#:   consumed.  Their fuel *output* is smaller by the conversion efficiency.
_PANEL_UNIT_NOTE = {
    "power-to-heat": "thermal",
    "electrolysis": "input-rated",
    "methanation": "input-rated",
    "Fischer-Tropsch": "input-rated",
}


#: Panels whose member list is longer than a useful heading.  Subplot titles
#: are centred over columns barely 150 px wide, so a long one runs into its
#: neighbour; the members are named in the legend either way.
_PANEL_TITLES: dict[tuple[str, ...], str] = {
    tuple(_POWER_TO_FUEL): "Power-to-fuel",
    ("onshore wind", "offshore wind"): "Wind",
}


def _panel_title(techs: list[str]) -> str:
    title = _PANEL_TITLES.get(
        tuple(techs), ", ".join(_smart_capitalize(t) for t in techs)
    )
    notes = sorted({_PANEL_UNIT_NOTE[t] for t in techs if t in _PANEL_UNIT_NOTE})
    if notes:
        title += f" ({', '.join(notes)})"
    return title


def _expand_capacity_group(group: list[str], index) -> list[str]:
    """Keep curated panel members, and append CCS siblings present in ``index``.

    ``CCGT`` therefore also shows ``CCGT CC`` when that carrier exists, without
    listing every capture-equipped plant in :data:`_POWER_GROUPS`.
    """
    names = list(group)
    seen = set(names)
    for extra in index:
        extra_s = str(extra)
        if extra_s in seen:
            continue
        parent = ccs_parent_carrier(extra_s)
        if parent in seen:
            names.append(extra_s)
            seen.add(extra_s)
    return names


def _faceted_capacity_chart(
    table: pd.DataFrame,
    groups: list[list[str]],
    *,
    unit: str,
    scale: float = _CAPACITY_SCALE,
    shared_y: bool = True,
    height: int = 800,
    fill_missing: bool = True,
    hover_fmt: str = "%{y:.3g}",
) -> go.Figure | None:
    """Legacy-style capacity panels: one subplot per technology group.

    Missing technologies still get a zero-height bar so the panel (e.g.
    Nuclear) is never a blank hole when the group is configured.

    ``fill_missing=False`` drops that filler instead.  Utilisation panels need
    it: a zero-height *capacity* bar honestly says "none installed", while a
    zero-height *capacity factor* says "installed and never used" — the
    opposite of the truth.
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

        for tech in _expand_capacity_group(tech_group, data.index):
            if tech in data.index:
                series = data.loc[tech, years]
            elif fill_missing:
                series = pd.Series(0.0, index=years)
            else:
                continue
            fig.add_trace(
                go.Bar(
                    x=years,
                    y=series.values,
                    name=_smart_capitalize(tech),
                    marker_color=resolve_tech_color(tech, palette),
                    hovertemplate=hover_fmt,
                ),
                row=row_idx,
                col=col_idx,
            )
            any_trace = True

    if not any_trace:
        return None

    for annotation in fig.layout.annotations:
        annotation.font.size = 14  # subplot titles: fit the column, see _PANEL_TITLES

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
        if _study_wide_panels(ctx, node)
        else _POWER_GROUPS
    )
    return _faceted_capacity_chart(table, groups, unit=section.unit)


def storage_capacities(ctx, node: str, section) -> go.Figure | None:
    """Stacked bar of all storage capacities."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "storage"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def ccs_capacities(ctx, node: str, section) -> go.Figure | None:
    """Installed capture plant, rated on the fuel-input side (not MW_e, not Mt)."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "ccs"),
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


# ---------------------------------------------------------------------------
# Utilisation: capacity factors and storage cycles
# ---------------------------------------------------------------------------

#: p.u. -> % for capacity-factor charts.
_UTILISATION_SCALE = 100.0

#: Cache miss sentinel.  ``None`` is a real cached value here ("this node has
#: no utilisation table"), and a DataFrame cannot be compared to a default with
#: ``!=`` without raising, so the marker has to be a distinct object.
_NOT_CACHED = object()


def _aligned_utilisation(ctx, node: str, kind: str) -> pd.DataFrame | None:
    """Utilisation restricted to the technologies the capacity chart shows.

    The two charts are read together, so a factor for a technology with no bar
    above it is a puzzle rather than information — and that happens for real:
    PyPSA-Eur parks some Links (oil boilers, ``OCGT methanol``, the ammonia
    cracker) at the pseudo-location ``EU`` in ``nodal_capacities.csv``, so they
    are absent from every *regional* capacity chart, while
    :func:`~pypsa2html.extract.tables.utilisation_table` reads the network and
    attributes them to the region they serve.  Rows the capacity chart does not
    carry are dropped here and logged; the reverse case (a capacity bar with no
    factor) stays as a gap.
    """
    # Two sections plot the same table; the network scan behind it is not free.
    cache_key = ("aligned_utilisation", node, kind)
    cached = getattr(ctx, "_files", {}).get(cache_key, _NOT_CACHED)
    if cached is not _NOT_CACHED:
        return None if cached is None else cached.copy()

    table = _compute_aligned_utilisation(ctx, node, kind)
    if hasattr(ctx, "_files"):
        ctx._files[cache_key] = table
    return None if table is None else table.copy()


def _compute_aligned_utilisation(ctx, node: str, kind: str) -> pd.DataFrame | None:
    table = utilisation_table(ctx, node, kind)
    if table is None or table.empty:
        return None
    capacities = capacity_table(ctx, node, kind)
    if capacities is None or capacities.empty:
        return table
    dropped = table.index.difference(capacities.index)
    if len(dropped):
        logger.info(
            "utilisation %s/%s: not on the capacity chart, dropped: %s",
            node,
            kind,
            ", ".join(str(d) for d in dropped),
        )
    aligned = table.reindex(index=capacities.index)
    aligned = aligned.loc[aligned.notna().any(axis=1)]
    return None if aligned.empty else aligned


def capacity_factors(ctx, node: str, section) -> go.Figure | None:
    """Capacity factor of every power technology, one cluster per technology."""
    table = _aligned_utilisation(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, (table * _UTILISATION_SCALE).T)
    return grouped_bar(
        table, unit=section.unit, title="", scale=_UTILISATION_SCALE,
        hover_fmt="%{y:.1f}",
    )


def capacity_factors_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Capacity factors in the same panels as ``capacities_by_tech``."""
    table = _aligned_utilisation(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, (table * _UTILISATION_SCALE).T)
    groups = (
        _POWER_GROUPS_AGGREGATE
        if _study_wide_panels(ctx, node)
        else _POWER_GROUPS
    )
    return _faceted_capacity_chart(
        table,
        groups,
        unit=section.unit,
        scale=_UTILISATION_SCALE,
        shared_y=False,
        fill_missing=False,
        hover_fmt="%{y:.1f}",
    )


def storage_cycles(ctx, node: str, section) -> go.Figure | None:
    """Equivalent full cycles per year of every storage technology.

    Energy discharged over ``e_nom_opt`` — the storage analogue of a capacity
    factor, and the number that says whether a store is a daily buffer
    (hundreds of cycles) or a seasonal one (a handful).
    """
    table = _aligned_utilisation(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return grouped_bar(table, unit=section.unit, title="", hover_fmt="%{y:.1f}")


def storage_cycles_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Storage cycles in the same panels as ``storage_capacities_by_tech``."""
    table = _aligned_utilisation(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return _faceted_capacity_chart(
        table,
        _STORAGE_GROUPS,
        unit=section.unit,
        scale=1.0,
        shared_y=False,
        height=500,
        fill_missing=False,
        hover_fmt="%{y:.1f}",
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
