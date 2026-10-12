"""Parameter sweeps: one metric read across a family of runs.

A sensitivity analysis is not a set of scenarios.  Six runs that differ only in
the investment cost of one technology answer a *single* question -- how does
the system respond to that number -- and the answer is a curve, not six
dashboards.  Listing them as scenarios buries the scenarios that genuinely
differ, and invites cross-reading cost totals that a repriced input makes
incomparable.

So a sweep point is declared on the scenario entry itself::

    scenarios:
      - name: run_nuc_4500
        results_dir: results/run_nuc_4500
        sensitivity: {sweep: nuclear_capex, value: 4500}

and the sweep says what the points mean::

    sensitivities:
      - id: nuclear_capex
        label: Nuclear investment cost
        parameter: {label: Overnight cost of new nuclear, unit: EUR/kW}
        nodes: [BEWAL]
        metrics:
          - {table: capacity, kind: power, rows: [nuclear], unit: GW}

Nothing here is model-specific: the metric names a table any PyPSA-Eur results
tree produces (:mod:`pypsa2html.extract.tables`) or an indicator frame, and the
rows are selected by label -- see :mod:`pypsa2html.metrics`, which reads the
same way for the key figures.

The sweep page is part of the comparison report: ``ctx`` is a
:class:`~pypsa2html.compare.CompareContext` and each point is read back from
its summary file, never from its networks.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from ..config import format_number
from ..metrics import metric_series
from .base import (
    CHART_HEIGHT,
    FONT_SIZE,
    HEADER_MARGIN_TOP,
    Html,
    add_chart_data,
    combine_charts,
    series_palette,
    strip_markup,
)

logger = logging.getLogger(__name__)

#: Dash and marker cycles, applied alongside colour.
#:
#: A sweep routinely produces series that lie exactly on top of each other --
#: every horizon before the technology becomes competitive returns the same
#: floor, and every point above the tipping price returns it too.  Drawn as
#: plain coloured lines they hide one another completely, so the reader sees
#: one curve and cannot tell whether the others are missing, zero, or equal.
#: A distinct dash makes coincident lines interleave rather than occlude, and
#: a distinct marker symbol keeps the data points tellable apart where they
#: land on the same pixel.
#: Menu labels for the two ways the same matrix is read.
VIEW_PARAMETER = "by parameter value"
VIEW_HORIZON = "by planning horizon"

DASHES: tuple[str, ...] = ("solid", "dash", "dot", "dashdot", "longdash", "longdashdot")
MARKERS: tuple[str, ...] = (
    "circle", "square", "diamond", "triangle-up", "x-thin-open", "star",
)


def sweep(ctx, node: str, section) -> go.Figure | Html | None:
    """Build the section of one declared sweep.

    ``node`` is the page's node and is deliberately *not* used to select the
    data: the sweep page is shared across regions, and the region a sweep is
    read for is pinned in its own ``nodes:`` key (default ``nodes.focus``).
    Following the region selector instead would silently re-point the question
    at, say, French nuclear the moment a reader changed the dropdown.
    """
    sweep_id = (getattr(section, "params", None) or {}).get("sweep")
    if not sweep_id:
        raise ValueError(
            f"section {section.id!r} has no 'sweep' parameter; sensitivity sections "
            "are generated from the config's 'sensitivities:' block"
        )
    spec = ctx.config.sensitivity(sweep_id)
    points = ctx.config.sweep_points(sweep_id)
    contexts = _point_data(ctx, sweep_id)
    if not contexts:
        return _note(
            f"No solved results for the {spec.label!r} sweep yet "
            f"({len(points)} point(s) declared)."
        )
    if len(contexts) < len(points):
        logger.warning(
            "sweep %s: %d of %d points have results; the curve is drawn over "
            "what is available",
            sweep_id,
            len(contexts),
            len(points),
        )

    targets = spec.nodes or [_default_node(ctx)]
    groups: list[tuple[str, go.Figure | None, go.Figure | None]] = []
    for metric in spec.metrics:
        for target in targets:
            frame = _sweep_frame(contexts, target, metric)
            if frame is None:
                continue
            label = _variant_label(spec, metric, target, ctx, multi=len(targets) > 1)
            add_chart_data(ctx, target, f"{section.title} - {label}", metric.unit, frame)
            caption = _caption(spec, contexts, target, ctx)
            groups.append(
                (
                    label,
                    _versus_parameter(frame, spec=spec, metric=metric, caption=caption),
                    _over_time(frame, spec=spec, metric=metric, caption=caption),
                )
            )

    if not groups:
        return _note(
            f"The {spec.label!r} sweep produced no readable metric for "
            f"{', '.join(targets)}."
        )

    # One metric read for one region -- the common case -- means the chart has
    # a single name, so the title carries it and the menu only has to say which
    # way round the numbers are read.  Spelling the metric into both put the
    # same long sentence in the title and in the menu button sitting beside it.
    single = len(groups) == 1
    figures: list[tuple[str, go.Figure | None]] = []
    for label, versus, over_time in groups:
        prefix = "" if single else f"{label} · "
        figures.append((f"{prefix}{VIEW_PARAMETER}", versus))
        figures.append((f"{prefix}{VIEW_HORIZON}", over_time))
    title = groups[0][0] if single else str(spec.label)
    return combine_charts(figures, title=title, menu_title="View")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _default_node(ctx) -> str:
    """Region a sweep is read for when the config does not pin one."""
    focus = getattr(ctx.config.nodes, "focus", None)
    if focus:
        return str(focus)
    return str(ctx.nodes.focus)


def _point_data(ctx, sweep_id: str) -> list:
    """``(point scenario, ScenarioData)`` for every sweep point with a summary.

    The comparison loads them once (``ctx.sweeps``); a point without a usable
    summary is absent, so the curve is drawn over what is available.
    """
    sweeps = getattr(ctx, "sweeps", None)
    if not isinstance(sweeps, dict):
        return []
    return list(sweeps.get(sweep_id, []))


def _sweep_frame(contexts, node: str, metric) -> pd.DataFrame | None:
    """Swept value x planning horizon matrix of one metric.

    Index is the parameter value (numeric, so the x axis is a real scale);
    columns are the year strings.
    """
    rows: dict[float, pd.Series] = {}
    for point, sctx in contexts:
        series = metric_series(sctx, node, metric)
        if series is None:
            logger.warning(
                "sweep point %s: metric %s unavailable for %s",
                point.name,
                metric.label,
                node,
            )
            continue
        rows[point.sensitivity.value] = series
    if not rows:
        return None
    frame = pd.DataFrame(rows).T.sort_index()
    frame.index.name = "value"
    frame.columns = [str(c) for c in frame.columns]
    return frame


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _axis_title(parameter) -> str:
    unit = strip_markup(parameter.unit)
    return f"{parameter.label} [{unit}]" if unit else str(parameter.label)


def _variant_label(spec, metric, node: str, ctx, *, multi: bool) -> str:
    """Dropdown button text: the metric, qualified by region when it varies."""
    if multi:
        return f"{metric.label} — {_node_label(ctx, node)}"
    return metric.label


def _node_label(ctx, node: str) -> str:
    for candidate in getattr(ctx, "nodes", []) or []:
        if getattr(candidate, "code", None) == node:
            return str(getattr(candidate, "label", node))
    return str(ctx.config.nodes.labels.get(node, node))


def _caption(spec, contexts, node: str, ctx) -> str:
    """The two facts the axes cannot carry: which region, and which points."""
    values = [format_number(p.sensitivity.value) for p, _ in contexts]
    unit = strip_markup(spec.parameter.unit)
    span = f"{values[0]}–{values[-1]}" if len(values) > 1 else values[0]
    runs = f"{len(values)} run{'s' if len(values) != 1 else ''}"
    return (
        f"{_node_label(ctx, node)} · {runs} · {spec.parameter.label} "
        f"{span}{' ' + unit if unit else ''}"
    )


def _add_caption(fig: go.Figure, caption: str) -> None:
    """The run list, under the title row rather than across it.

    ``y`` is in paper units, so it has to clear the strip
    :data:`~pypsa2html.charts.base.HEADER_MARGIN_TOP` reserves for the title
    and the variant menu; at the old 1.10 it was printed straight through the
    title.
    """
    if not caption:
        return
    fig.add_annotation(
        text=caption,
        xref="paper",
        yref="paper",
        x=0,
        y=1.045,
        xanchor="left",
        showarrow=False,
        align="left",
        font={"size": FONT_SIZE - 3, "color": "#555555"},
    )


def _series_style(index: int, colour: str | None) -> dict:
    """Colour, dash and marker for series ``index`` -- see :data:`DASHES`."""
    return {
        "line": {"width": 2.4, "color": colour, "dash": DASHES[index % len(DASHES)]},
        "marker": {
            "size": 9,
            "symbol": MARKERS[index % len(MARKERS)],
            "line": {"width": 1.4, "color": "#ffffff"},
        },
    }


def _layout(fig: go.Figure, *, x_title: str, y_title: str, tickvals) -> None:
    fig.update_layout(
        hovermode="x unified",
        height=CHART_HEIGHT,
        legend_title_text="",
        xaxis_title=x_title,
        yaxis_title=y_title,
        font={"size": FONT_SIZE},
        margin={"l": 70, "r": 30, "t": HEADER_MARGIN_TOP, "b": 60},
        xaxis={"tickmode": "array", "tickvals": list(tickvals)},
    )


def _versus_parameter(frame: pd.DataFrame, *, spec, metric, caption: str) -> go.Figure | None:
    """The sweep itself: swept value on x, one line per planning horizon."""
    if frame is None or frame.empty:
        return None
    values = [float(v) for v in frame.index]
    palette = series_palette(list(frame.columns))
    fig = go.Figure()
    for i, year in enumerate(frame.columns):
        fig.add_trace(
            go.Scatter(
                x=values,
                y=frame[year].to_numpy(),
                name=str(year),
                mode="lines+markers",
                connectgaps=False,
                hovertemplate="%{y:.3g}",
                **_series_style(i, palette.get(str(year))),
            )
        )
    _layout(
        fig,
        x_title=_axis_title(spec.parameter),
        y_title=strip_markup(metric.unit),
        tickvals=values,
    )
    _add_caption(fig, caption)
    return fig


def _over_time(frame: pd.DataFrame, *, spec, metric, caption: str) -> go.Figure | None:
    """The same numbers read the other way: horizon on x, one line per point."""
    if frame is None or frame.empty:
        return None
    unit = strip_markup(spec.parameter.unit)
    transposed = frame.T
    labels = [
        f"{format_number(v)}{' ' + unit if unit else ''}" for v in transposed.columns
    ]
    transposed.columns = labels
    palette = series_palette(labels)
    years = [str(y) for y in transposed.index]
    fig = go.Figure()
    for i, label in enumerate(labels):
        fig.add_trace(
            go.Scatter(
                x=years,
                y=transposed[label].to_numpy(),
                name=label,
                mode="lines+markers",
                connectgaps=False,
                hovertemplate="%{y:.3g}",
                **_series_style(i, palette.get(label)),
            )
        )
    _layout(fig, x_title="Planning horizon", y_title=strip_markup(metric.unit),
            tickvals=years)
    _add_caption(fig, caption)
    return fig


def _note(message: str) -> Html:
    """A visible, neutral statement in place of a chart with nothing to draw.

    Returning ``None`` would drop the section, and a page whose every section
    is dropped is not written at all -- leaving the navigation entry that the
    manifest already rendered into every other page pointing at nothing.
    """
    logger.warning("sensitivity section: %s", message)
    return Html(f'<div class="p2h-desc">{message}</div>')
