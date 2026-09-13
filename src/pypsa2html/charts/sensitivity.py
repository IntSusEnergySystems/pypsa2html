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
rows are selected by label.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from .. import indicators
from ..config import format_number
from ..context import BuildContext, build_context
from ..extract.tables import capacity_table, cost_table
from .base import (
    CHART_HEIGHT,
    FONT_SIZE,
    Html,
    add_chart_data,
    combine_charts,
    series_palette,
    strip_markup,
)

logger = logging.getLogger(__name__)


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
    contexts = _point_contexts(ctx, points)
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
    figures: list[tuple[str, go.Figure | None]] = []
    for metric in spec.metrics:
        for target in targets:
            frame = _sweep_frame(contexts, target, metric)
            if frame is None:
                continue
            label = _variant_label(spec, metric, target, ctx, multi=len(targets) > 1)
            add_chart_data(ctx, target, f"{section.title} - {label}", metric.unit, frame)
            caption = _caption(spec, contexts, target, ctx)
            figures.append(
                (label, _versus_parameter(frame, spec=spec, metric=metric, caption=caption))
            )
            figures.append(
                (
                    f"{label} (over time)",
                    _over_time(frame, spec=spec, metric=metric, caption=caption),
                )
            )

    if not figures:
        return _note(
            f"The {spec.label!r} sweep produced no readable metric for "
            f"{', '.join(targets)}."
        )
    return combine_charts(figures, menu_title="View")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _default_node(ctx) -> str:
    """Region a sweep is read for when the config does not pin one."""
    focus = getattr(ctx.config.nodes, "focus", None)
    if focus:
        return str(focus)
    return str(ctx.nodes.focus)


def _point_contexts(ctx, points) -> list[tuple[object, BuildContext]]:
    """``(point scenario, context)`` for every sweep point with results on disk.

    Reuses the site-wide context pool, so a sweep read from eight scenarios'
    copies of the shared page loads each point's networks once.
    """
    key = ("sensitivity_contexts", tuple(p.name for p in points))
    cached = ctx._files.get(key)
    if cached is not None:
        return cached

    pool = ctx._files.get(("_context_pool",))
    config = ctx.config
    out: list[tuple[object, BuildContext]] = []
    for point in points:
        results_dir = config.results_dir(point.name)
        if not results_dir.exists():
            logger.warning(
                "sweep point %s: results_dir missing, skipping: %s",
                point.name,
                results_dir,
            )
            continue
        try:
            if isinstance(pool, dict):
                if point.name not in pool:
                    sctx = build_context(config, point.name)
                    sctx._files[("_context_pool",)] = pool
                    pool[point.name] = sctx
                out.append((point, pool[point.name]))
            else:
                out.append((point, build_context(config, point.name)))
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("sweep point %s: %s", point.name, exc)
            continue

    ctx._files[key] = out
    return out


def _metric_table(sctx: BuildContext, node: str, metric) -> pd.DataFrame | None:
    """The table a metric reads, as rows x year-string columns."""
    if metric.table == "capacity":
        return capacity_table(sctx, node, metric.kind)
    if metric.table == "cost":
        return cost_table(sctx, node, metric.kind)
    if metric.table == "indicator":
        data = indicators.for_node(sctx, node)
        if data is None:
            return None
        if not hasattr(data, metric.field):
            raise ValueError(
                f"indicator frame {metric.field!r} does not exist; available: "
                f"{_indicator_fields(data)}"
            )
        frame = getattr(data, metric.field)
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(
                f"indicator {metric.field!r} is a {type(frame).__name__}, not a "
                f"table of rows by year; a sweep metric needs one of "
                f"{_indicator_fields(data)}"
            )
        # Indicator frames are years x codes; every other table is rows x years.
        out = frame.T
        out.columns = [str(c) for c in out.columns]
        return out
    raise ValueError(f"unsupported sensitivity metric table {metric.table!r}")


def _indicator_fields(data) -> list[str]:
    """Public attributes of an ``Indicators`` object that are plain tables."""
    out = []
    for name in sorted(dir(data)):
        if name.startswith("_"):
            continue
        try:
            if isinstance(getattr(data, name), pd.DataFrame):
                out.append(name)
        except Exception:  # noqa: BLE001 - a broken frame must not mask the error
            continue
    return out


def _select_rows(table: pd.DataFrame, rows: list[str]) -> pd.DataFrame:
    """Rows of ``table`` named by ``rows``, matched case-insensitively."""
    if not rows:
        return table
    lookup: dict[str, list] = {}
    for label in table.index:
        lookup.setdefault(str(label).strip().lower(), []).append(label)
    keep: list = []
    for wanted in rows:
        found = lookup.get(str(wanted).strip().lower())
        if found:
            keep.extend(found)
    return table.loc[keep] if keep else table.iloc[0:0]


def _metric_series(sctx: BuildContext, node: str, metric) -> pd.Series | None:
    """One sweep point's metric, indexed by year string.

    A row the config asks for but the point does not carry reads **zero**, not
    missing: the optimiser built none of it, which is a value on the curve.
    A point with no table at all reads missing, so the line breaks rather than
    dropping to zero and inventing a result.
    """
    table = _metric_table(sctx, node, metric)
    if table is None:
        return None
    years = [str(y) for y in sctx.year_columns]
    selected = _select_rows(table, metric.rows)
    if metric.rows and selected.empty:
        logger.info(
            "sweep metric %s/%s: no row of %s matched %s in %s; reading zero",
            metric.table,
            metric.kind or metric.field,
            node,
            metric.rows,
            sctx.scenario.name,
        )
    selected = selected.reindex(columns=years, fill_value=0.0)
    return selected.sum(axis=0).astype(float) * float(metric.scale)


def _sweep_frame(contexts, node: str, metric) -> pd.DataFrame | None:
    """Swept value x planning horizon matrix of one metric.

    Index is the parameter value (numeric, so the x axis is a real scale);
    columns are the year strings.
    """
    rows: dict[float, pd.Series] = {}
    for point, sctx in contexts:
        series = _metric_series(sctx, node, metric)
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
    if not caption:
        return
    fig.add_annotation(
        text=caption,
        xref="paper",
        yref="paper",
        x=0,
        y=1.10,
        xanchor="left",
        showarrow=False,
        align="left",
        font={"size": FONT_SIZE - 3, "color": "#555555"},
    )


def _layout(fig: go.Figure, *, x_title: str, y_title: str, tickvals) -> None:
    fig.update_layout(
        hovermode="x unified",
        height=CHART_HEIGHT,
        legend_title_text="",
        xaxis_title=x_title,
        yaxis_title=y_title,
        font={"size": FONT_SIZE},
        margin={"l": 60, "r": 30, "t": 60, "b": 55},
        xaxis={"tickmode": "array", "tickvals": list(tickvals)},
    )


def _versus_parameter(frame: pd.DataFrame, *, spec, metric, caption: str) -> go.Figure | None:
    """The sweep itself: swept value on x, one line per planning horizon."""
    if frame is None or frame.empty:
        return None
    values = [float(v) for v in frame.index]
    palette = series_palette(list(frame.columns))
    fig = go.Figure()
    for year in frame.columns:
        fig.add_trace(
            go.Scatter(
                x=values,
                y=frame[year].to_numpy(),
                name=str(year),
                mode="lines+markers",
                line={"width": 2, "color": palette.get(str(year))},
                marker={"size": 8},
                connectgaps=False,
                hovertemplate="%{y:.3g}",
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
    for label in labels:
        fig.add_trace(
            go.Scatter(
                x=years,
                y=transposed[label].to_numpy(),
                name=label,
                mode="lines+markers",
                line={"width": 2, "color": palette.get(label)},
                marker={"size": 8},
                connectgaps=False,
                hovertemplate="%{y:.3g}",
            )
        )
    _layout(fig, x_title="", y_title=strip_markup(metric.unit), tickvals=years)
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
