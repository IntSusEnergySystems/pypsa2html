"""The comparison report: several scenarios, read from their summary files.

A scenario report knows nothing of its siblings (D24).  This module builds the
report that does: given a list of result folders (or the scenarios of a
config) it reads each one's ``summary.json`` / ``summary.csv`` and draws them
side by side -- the key figures with their difference to a reference
scenario, the comparison charts, the parameter sweeps, and a provenance page
saying where every number comes from and whether the runs belong together.

It never loads a solved network unless asked to (``recompute``), so it runs
in seconds over any number of scenarios, and it can combine trees solved on
different machines by copying their summary files.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from .config import Config, ScenarioConfig
from .datafiles import load_taxonomy
from .metrics import KPI, SYSTEM, evaluate_kpis
from .nodes import Node, NodeSet
from .summary import (
    MISSING,
    STALE,
    SUMMARY_JSON,
    SummaryScenarioData,
    SummaryStatus,
    check_summary,
    read_meta,
    summary_path,
)

logger = logging.getLogger(__name__)

#: Where a results tree keeps its scenario report, tried in order.
_SUMMARY_LOCATIONS = ("", "html", "html/pypsa")


@dataclass
class ScenarioSource:
    """One scenario of the comparison: where its summary is and what it says."""

    name: str
    label: str
    summary: Path | None = None
    results_dir: Path | None = None
    configured: ScenarioConfig | None = None
    data: SummaryScenarioData | None = None
    meta: dict | None = None
    status: SummaryStatus | None = None
    note: str = ""

    @property
    def sweep(self):
        if self.configured is not None and self.configured.sensitivity is not None:
            return self.configured.sensitivity
        return None

    @property
    def usable(self) -> bool:
        return self.data is not None


# ---------------------------------------------------------------------------
# Resolving the scenarios
# ---------------------------------------------------------------------------

def _find_summary(directory: Path) -> Path | None:
    for sub in _SUMMARY_LOCATIONS:
        candidate = (directory / sub / SUMMARY_JSON) if sub else directory / SUMMARY_JSON
        if candidate.exists():
            return candidate
    return None


def _from_config(config: Config, scenario: ScenarioConfig) -> ScenarioSource:
    results_dir = config.results_dir(scenario.name)
    summary = config.summary_dir(scenario.name) / SUMMARY_JSON
    if not summary.exists():
        found = _find_summary(results_dir) if results_dir.exists() else None
        summary = found or summary
    return ScenarioSource(
        name=scenario.name,
        label=scenario.label,
        summary=summary,
        results_dir=results_dir,
        configured=scenario,
    )


def _from_path(config: Config, raw: str | Path) -> ScenarioSource:
    """A source named by a path: a results tree, an HTML folder or a summary."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path)
    path = path.resolve()
    summary = summary_path(path) if path.is_file() else (
        _find_summary(path) if path.is_dir() else None
    )
    meta = read_meta(summary) if summary else None
    by_dir = {config.results_dir(s.name): s for s in config.scenarios}
    configured = by_dir.get(path) if path.is_dir() else None
    if configured is None and meta is not None:
        name = (meta.get("scenario") or {}).get("name")
        configured = next((s for s in config.scenarios if s.name == name), None)
    if summary is None and configured is not None:
        candidate = config.summary_dir(configured.name) / SUMMARY_JSON
        summary = candidate if candidate.exists() else None
        meta = read_meta(summary) if summary else None

    if meta is not None:
        name = str((meta.get("scenario") or {}).get("name") or path.name)
        label = str((meta.get("scenario") or {}).get("label") or name)
    elif configured is not None:
        name, label = configured.name, configured.label
    else:
        name = label = path.name if path.is_dir() else path.parent.name
    if configured is not None and configured.name == name:
        label = configured.label

    results_dir: Path | None = None
    if path.is_dir() and (path / "networks").is_dir():
        results_dir = path
    elif meta is not None:
        recorded = (meta.get("scenario") or {}).get("results_dir")
        if recorded and Path(recorded).exists():
            results_dir = Path(recorded)
    if results_dir is None and configured is not None:
        candidate = config.results_dir(configured.name)
        results_dir = candidate if candidate.exists() else None
    return ScenarioSource(
        name=name,
        label=label,
        summary=summary if summary is not None else (
            path / SUMMARY_JSON if path.is_dir() else path
        ),
        results_dir=results_dir,
        configured=configured if configured is not None and configured.name == name else None,
    )


def resolve_sources(
    config: Config,
    sources: list[str | Path] | None = None,
    *,
    names: list[str] | None = None,
) -> list[ScenarioSource]:
    """The scenarios to compare, in order (sweep points excluded).

    ``sources`` are paths (results trees, HTML folders or ``summary.json``
    files); ``names`` are configured scenario names; with neither, the
    ``compare.scenarios`` list, else every browsable configured scenario.
    """
    if sources:
        out = [_from_path(config, p) for p in sources]
    else:
        wanted = names or config.compare.scenarios or config.report_scenario_names
        out = [_from_config(config, config.scenario(n)) for n in wanted]
    out = [s for s in out if s.sweep is None]
    # Names must be unique: they key the data, the files and the CSV.
    seen: dict[str, int] = {}
    for source in out:
        count = seen.get(source.name, 0)
        seen[source.name] = count + 1
        if count:
            parent = source.results_dir.parent.name if source.results_dir else str(count)
            source.name = f"{parent}/{source.name}"
            source.label = f"{source.label} ({parent})"
    labels: dict[str, int] = {}
    for source in out:
        labels[source.label] = labels.get(source.label, 0) + 1
    for source in out:
        if labels[source.label] > 1:
            source.label = f"{source.label} [{source.name}]"
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _recompute(config: Config, source: ScenarioSource, *, write: bool) -> bool:
    """Rebuild ``source``'s summary from its results tree.  ``True`` on success."""
    from .context import build_context
    from .networks import clear_path_cache
    from .summary import build_summary, write_summary

    if source.results_dir is None or not source.results_dir.exists():
        source.note = "cannot recompute: results tree not reachable"
        return False
    cfg = config
    if source.configured is None:
        cfg = config.with_scenario(source.name, source.results_dir, label=source.label)
    logger.info("recomputing the summary of %s from %s", source.name, source.results_dir)
    try:
        ctx = build_context(cfg, source.name)
        if write:
            target = cfg.summary_dir(source.name) if source.configured else (
                source.summary.parent if source.summary else source.results_dir
            )
            path = write_summary(ctx, target, cfg.kpis)
            source.summary = path
            source.data = SummaryScenarioData.load(path, results_dir=source.results_dir)
            source.meta = source.data.meta
        else:
            meta, frame = build_summary(ctx, cfg.kpis)
            source.data = SummaryScenarioData.from_frame(meta, frame, source.results_dir)
            source.meta = meta
    except Exception as exc:  # noqa: BLE001 - one scenario must not sink the report
        logger.exception("recomputing %s failed", source.name)
        source.note = f"recompute failed: {type(exc).__name__}: {exc}"
        return False
    finally:
        clear_path_cache()
    source.status = SummaryStatus("ok", ["recomputed from the results tree"])
    source.note = "recomputed"
    return True


def load_sources(
    config: Config,
    sources: list[ScenarioSource],
    *,
    recompute: bool = False,
    write_summaries: bool = False,
) -> list[ScenarioSource]:
    """Read every source's summary and judge it (see :func:`check_summary`).

    A missing or incompatible summary is skipped -- or recomputed with
    ``recompute``.  A stale one is used with a warning under ``on_stale:
    warn``, skipped under ``skip``, and raises under ``fail``.
    """
    policy = config.compare.on_stale
    for source in sources:
        source.meta = read_meta(source.summary) if source.summary else None
        source.status = check_summary(source.meta, source.results_dir, config)
        status = source.status.status
        if status in (MISSING, "incompatible") or (status == STALE and recompute):
            if recompute and _recompute(config, source, write=write_summaries):
                continue
            message = f"{source.name}: {status} summary ({'; '.join(source.status.reasons)})"
            if policy == "fail":
                raise FileNotFoundError(
                    message + " -- run `pypsa2html summarize` or `compare --recompute`"
                )
            logger.warning("%s; skipped", message)
            source.note = source.note or "skipped"
            continue
        if status == STALE:
            message = f"{source.name}: stale summary ({'; '.join(source.status.reasons[:3])})"
            if policy == "fail":
                raise ValueError(message)
            if policy == "skip":
                logger.warning("%s; skipped", message)
                source.note = "skipped (stale)"
                continue
            logger.warning("%s; used anyway", message)
        source.data = SummaryScenarioData.load(source.summary, results_dir=source.results_dir)
        if source.configured is not None:
            source.data.label = source.label
    return sources


# ---------------------------------------------------------------------------
# The comparison context
# ---------------------------------------------------------------------------

def _node_set(config: Config, scenarios: list[SummaryScenarioData]) -> NodeSet:
    """Union of the scenarios' nodes, in the reference's order."""
    seen: dict[str, Node] = {}
    for data in scenarios:
        for record in data.nodes:
            code = str(record["code"])
            if code in seen:
                continue
            members = record.get("members")
            seen[code] = Node(
                code=code,
                label=str(config.nodes.labels.get(code, record.get("label") or code)),
                aggregate=bool(record.get("aggregate")),
                members=tuple(members) if members is not None else None,
            )
    nodes = list(seen.values())
    if not nodes:
        raise ValueError("the compared summaries list no node")
    focus = config.nodes.focus if config.nodes.focus in seen else None
    if focus is None and scenarios:
        focus = scenarios[0].meta.get("focus")
    if focus not in seen:
        focus = nodes[0].code
    aggregate = config.nodes.aggregate.code if config.nodes.aggregate.enabled else None
    return NodeSet(nodes=nodes, focus=focus, aggregate_code=aggregate)


@dataclass
class CompareContext:
    """What the comparison page builders get in place of a ``BuildContext``.

    ``scenarios`` maps a *label* to a :class:`SummaryScenarioData`, in display
    order with the reference first.  ``config``, ``taxonomy`` and ``_files``
    make the shared chart helpers (labels, colours, chart data) work as on a
    scenario page.
    """

    config: Config
    taxonomy: object
    scenarios: dict[str, SummaryScenarioData]
    sources: list[ScenarioSource]
    reference: str | None
    nodes: NodeSet
    out_dir: Path
    kpis: list[KPI]
    sweeps: dict[str, list[tuple[ScenarioConfig, SummaryScenarioData]]] = field(
        default_factory=dict
    )
    _files: dict = field(default_factory=dict, repr=False)

    @property
    def scenario(self):
        compare = self.config.compare
        return SimpleNamespace(name=compare.name, label=compare.title, description=None)

    @property
    def year_columns(self) -> list[str]:
        years: list[str] = []
        for data in self.scenarios.values():
            for y in data.year_columns:
                if y not in years:
                    years.append(y)
        return sorted(years, key=lambda y: (not y.isdigit(), int(y) if y.isdigit() else 0, y))

    @property
    def horizons(self) -> list[int]:
        return [int(y) for y in self.year_columns if y.isdigit()]

    def source(self, label: str) -> ScenarioSource | None:
        return next((s for s in self.sources if s.label == label and s.usable), None)

    def kpi_values(self, node: str) -> dict[str, pd.DataFrame]:
        """``{scenario label: KPI x year table}`` for ``node`` (or ``SYSTEM``)."""
        key = ("compare_kpis", str(node))
        if key not in self._files:
            self._files[key] = {
                label: evaluate_kpis(data, node, self.kpis)
                for label, data in self.scenarios.items()
            }
        return self._files[key]

    def report_href(self, label: str, node: str | None = None,
                    page: str = "key_figures") -> str | None:
        """Link from the comparison to one scenario's own report."""
        source = self.source(label)
        if source is None:
            return None
        template = self.config.compare.report_url
        if template:
            return template.format(scenario=source.name, node=node or self.nodes.focus)
        if source.summary is None or not Path(source.summary).exists():
            return None
        folder = Path(source.summary).parent
        name = source.name.rsplit("/", 1)[-1]
        target = folder / f"{node or self.nodes.focus}_{page}_{name}.html"
        if not target.exists():
            target = folder / "index.html"
            if not target.exists():
                return None
        return os.path.relpath(target, self.out_dir).replace(os.sep, "/")


def _reference(sources: list[ScenarioSource], wanted: str | None) -> ScenarioSource | None:
    usable = [s for s in sources if s.usable]
    if not usable:
        return None
    if wanted:
        for source in usable:
            if wanted in (source.name, source.label):
                return source
        logger.warning(
            "reference scenario %r is not among the compared scenarios (%s); "
            "using %s", wanted, [s.name for s in usable], usable[0].name,
        )
    return usable[0]


def _load_sweeps(config: Config, *, recompute: bool, write_summaries: bool):
    """``{sweep id: [(point, data)]}`` of every declared sweep with a page.

    Sweep points never fail the report: a point without a usable summary is
    left out with a warning and the curve is drawn over the others.
    """
    out: dict[str, list] = {}
    if "sensitivity" not in config.compare.pages:
        return out
    saved = config.compare.on_stale
    config.compare.on_stale = "warn" if saved == "fail" else saved
    try:
        for sweep in config.sensitivities:
            points = [_from_config(config, p) for p in config.sweep_points(sweep.id)]
            load_sources(config, points, recompute=recompute, write_summaries=write_summaries)
            out[sweep.id] = [(p.configured, p.data) for p in points if p.usable]
    finally:
        config.compare.on_stale = saved
    return out


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def comparison_table(ctx: CompareContext) -> pd.DataFrame:
    """Every KPI of every scenario and node, with its difference to the reference.

    The long table behind the key-figures pages, written as
    ``comparison.csv``: what the batch logs used to assemble by hand.
    """
    rows = []
    by_id = {k.id: k for k in ctx.kpis}
    for node in [SYSTEM, *ctx.nodes.codes]:
        values = ctx.kpi_values(node)
        reference = values.get(ctx.reference) if ctx.reference else None
        for label, table in values.items():
            source = ctx.source(label)
            for kpi_id, series in table.iterrows():
                kpi = by_id[kpi_id]
                for year, value in series.items():
                    if pd.isna(value):
                        continue
                    ref = (
                        reference.at[kpi_id, year]
                        if reference is not None and kpi_id in reference.index
                        and year in reference.columns else float("nan")
                    )
                    delta = value - ref if pd.notna(ref) else float("nan")
                    rows.append({
                        "scenario": source.name if source else label,
                        "label": label,
                        "node": node,
                        "group": kpi.group,
                        "kpi": kpi_id,
                        "indicator": kpi.label.strip(),
                        "unit": kpi.unit,
                        "year": year,
                        "value": value,
                        "reference": ref,
                        "delta": delta,
                        "delta_pct": (delta / abs(ref) * 100.0)
                        if pd.notna(delta) and ref not in (0, 0.0) else float("nan"),
                    })
    return pd.DataFrame(rows)


def _compare_render_kwargs(ctx: CompareContext):
    n = len(ctx.scenarios)
    reference = ctx.reference or ""
    footer = (
        f"comparison of {n} scenario{'s' if n != 1 else ''}"
        + (f" &middot; reference <strong>{reference}</strong>" if reference else "")
    )

    def kwargs(_ctx, _page) -> dict:
        return {"subtitle": ctx.config.compare.title, "footer": footer}

    return kwargs


def compare_site(
    config: Config,
    sources: list[str | Path] | None = None,
    *,
    names: list[str] | None = None,
    reference: str | None = None,
    out_dir: str | Path | None = None,
    recompute: bool = False,
    write_summaries: bool = False,
    only_pages: list[str] | None = None,
):
    """Build the comparison report.  Returns a :class:`~pypsa2html.build.BuildReport`.

    ``sources`` (paths) or ``names`` (configured scenarios) choose what is
    compared -- see :func:`resolve_sources`.  ``reference`` overrides
    ``compare.reference``.  ``recompute`` rebuilds a missing, stale or
    incompatible summary from its results tree (loading its networks, one
    scenario at a time); ``write_summaries`` also writes it back.
    """
    from .build import BuildReport, render_site
    from .pages import load_manifest
    from .report.render import write_index

    started = time.perf_counter()
    report = BuildReport()
    out = Path(out_dir).resolve() if out_dir else config.compare_dir()

    resolved = resolve_sources(config, sources, names=names)
    if not resolved:
        raise ValueError("nothing to compare: no scenario given or configured")
    load_sources(config, resolved, recompute=recompute, write_summaries=write_summaries)
    ref = _reference(resolved, reference or config.compare.reference)
    usable = [s for s in resolved if s.usable]
    if ref is not None:
        usable = [ref] + [s for s in usable if s is not ref]
    sweeps = _load_sweeps(config, recompute=recompute, write_summaries=write_summaries)
    if not usable and not any(sweeps.values()):
        raise ValueError(
            "no compared scenario has a usable summary: build the scenario reports "
            "first, or run `pypsa2html compare --recompute`"
        )

    datas = [s.data for s in usable] or [d for pts in sweeps.values() for _, d in pts]
    ctx = CompareContext(
        config=config,
        taxonomy=load_taxonomy(),
        scenarios={s.label: s.data for s in usable},
        sources=resolved,
        reference=ref.label if ref is not None else None,
        nodes=_node_set(config, datas),
        out_dir=out,
        kpis=list(config.kpis),
        sweeps=sweeps,
    )
    if len(usable) == 1:
        logger.warning(
            "the comparison holds a single scenario (%s); differences are empty",
            usable[0].name,
        )

    manifest = load_manifest(
        enable=config.plots,
        include_pages=config.compare.pages,
        sensitivities=config.sensitivities,
        site="compare",
    )
    if not usable:
        manifest.pages = [p for p in manifest.pages if p.id in ("sensitivity", "scenarios")]
    tag = config.compare.name
    render_site(
        ctx,
        manifest,
        out,
        file_tag=tag,
        report=report,
        report_name=tag,
        only_pages=only_pages,
        render_kwargs=_compare_render_kwargs(ctx),
    )

    if usable:
        table = comparison_table(ctx)
        csv_path = out / "comparison.csv"
        table.to_csv(csv_path, index=False, float_format="%.10g")
        report.written.append(csv_path)
        logger.info("wrote %s (%d rows)", csv_path, len(table))

    if report.written and manifest.pages:
        node = config.compare.landing.get("node") or ctx.nodes.focus
        page_id = config.compare.landing.get("page") or "key_figures"
        page = next((p for p in manifest.pages if p.id == page_id), manifest.pages[0])
        target = page.filename(node, tag)
        report.index = write_index(out, target)
        report.written.append(report.index)
        logger.info("comparison entry point: %s -> %s", report.index, target)

    report.seconds += time.perf_counter() - started
    logger.info(
        "comparison of %d scenario(s) (reference %s) in %s, %.1fs",
        len(usable), ctx.reference, out, report.seconds,
    )
    return report


def vintage_spread(ctx: CompareContext) -> tuple[datetime.datetime, datetime.datetime] | None:
    """Earliest and latest solve date of the compared scenarios."""
    stamps = []
    for source in ctx.sources:
        if not source.usable:
            continue
        solved = (source.data.meta or {}).get("solved_at")
        if solved:
            try:
                stamps.append(datetime.datetime.fromisoformat(solved))
            except ValueError:
                continue
    if not stamps:
        return None
    return min(stamps), max(stamps)
