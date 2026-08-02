"""The build loop: manifest x nodes x scenarios -> a navigable site.

This replaces ``create_combined_chart_country`` (200 lines in which the
fragment names, TOC entries, section ids and plot-toggle keys were hardcoded in
four parallel places) and the 13 kB of hand-written ``cp`` commands in
``build_folder.sh``.

A section whose builder is missing, raises, or returns ``None`` becomes a
visible placeholder rather than a ``NameError`` that kills the whole run --
the legacy code dereferenced variables defined inside disabled ``if`` branches,
so switching any single plot off broke page assembly entirely.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .context import BuildContext, build_context
from .pages import Manifest, Page, Section, load_manifest
from .report.render import (
    RenderedSection,
    figure_to_html,
    missing_section,
    render_page,
    write_index,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildReport:
    """What a build produced, for logging and for the tests."""

    written: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        return (
            f"{len(self.written)} page(s) written, {len(self.skipped)} section(s) "
            f"skipped, {len(self.failed)} section(s) failed, in {self.seconds:.1f}s"
        )


def resolve_builder(dotted: str):
    """Resolve a manifest ``builder`` string to a callable.

    ``"sepia.fec_by_sector"`` -> ``pypsa2html.charts.sepia.fec_by_sector``.
    Returns ``None`` (with a warning) when the module or attribute is absent,
    so a partially implemented manifest still renders.
    """
    module_name, _, func_name = dotted.rpartition(".")
    if not module_name:
        raise ValueError(f"builder {dotted!r} must be of the form '<module>.<function>'")
    try:
        module = importlib.import_module(f".charts.{module_name}", package=__package__)
    except ImportError as exc:
        logger.warning("builder module 'charts.%s' unavailable: %s", module_name, exc)
        return None
    func = getattr(module, func_name, None)
    if func is None:
        logger.warning("builder '%s' not found in charts.%s", func_name, module_name)
    return func


def _narrative(config: Config, section_id: str, node: str) -> str:
    """Narrative HTML for a section, resolved node -> default.

    The legacy ``get_country_specific_desc`` filtered by an ``EU_``/``BE_``
    prefix but the call sites looked up ``f"{country}_{slug}"``, so every
    country other than BE and EU silently rendered with empty descriptions and
    the generic fallback text in the file was unreachable.
    """
    entry = config.texts.get(section_id)
    if entry is None:
        return ""
    if isinstance(entry, str):
        return entry
    return str(entry.get(node) or entry.get("default") or "")


def _build_section(ctx: BuildContext, node, section: Section) -> RenderedSection | None:
    builder = resolve_builder(section.builder)
    if builder is None:
        return missing_section(section, f"builder '{section.builder}' is not implemented yet")
    try:
        result = builder(ctx, node.code, section)
    except Exception as exc:  # a broken chart must not sink the report
        logger.exception("section %s failed for node %s", section.id, node.code)
        return missing_section(section, f"{type(exc).__name__}: {exc}")

    if result is None:
        return None
    # A builder returns either a plotly Figure or already-rendered HTML
    # (charts.base.Html, or anything exposing __html__ / being a str).
    if hasattr(result, "__html__"):
        body = result.__html__()
    elif isinstance(result, str):
        body = result
    else:
        body = figure_to_html(result, plotly=ctx.config.output.plotly)
    return RenderedSection(
        id=section.id,
        title=section.title,
        body=body,
        description=_narrative(ctx.config, section.id, node.code),
    )


def build_scenario(
    config: Config,
    scenario_name: str,
    *,
    manifest: Manifest | None = None,
    ctx: BuildContext | None = None,
    report: BuildReport | None = None,
) -> BuildReport:
    """Build every page for every node of one scenario."""
    started = time.perf_counter()
    report = report or BuildReport()
    ctx = ctx or build_context(config, scenario_name)
    manifest = manifest or load_manifest(
        enable=config.plots, include_pages=config.output.pages
    )
    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    shared_done: set[str] = set()

    for page in manifest:
        shared = page.shared and config.output.shared_maps
        for node in ctx.nodes:
            if shared and page.id in shared_done:
                continue

            sections: list[RenderedSection] = []
            for section in page.sections:
                if not section.applies_to(node):
                    continue
                rendered = _build_section(ctx, node, section)
                if rendered is None:
                    report.skipped.append((scenario_name, node.code, section.id))
                    continue
                if "p2h-missing" in rendered.body:
                    report.failed.append((scenario_name, node.code, section.id))
                sections.append(rendered)

            if not sections:
                logger.info("page %s/%s has no content, not written", node.code, page.id)
                continue

            html = render_page(
                page=page,
                manifest=manifest,
                sections=sections,
                node=node,
                nodes=ctx.nodes,
                scenario=ctx.scenario,
                scenarios=config.scenarios,
                project=config.project,
                plotly=config.output.plotly,
            )
            path = out_dir / page.filename(node.code, scenario_name)
            path.write_text(html, encoding="utf-8")
            report.written.append(path)
            logger.info("wrote %s (%.0f kB)", path.name, path.stat().st_size / 1024)

            if shared:
                shared_done.add(page.id)
                break

    report.seconds += time.perf_counter() - started
    return report


def build_site(
    config: Config,
    *,
    scenarios: list[str] | None = None,
    manifest: Manifest | None = None,
) -> BuildReport:
    """Build the whole site: every scenario, plus ``index.html``."""
    report = BuildReport()
    manifest = manifest or load_manifest(
        enable=config.plots, include_pages=config.output.pages
    )
    names = scenarios or config.scenario_names

    for name in names:
        logger.info("=== scenario %s ===", name)
        build_scenario(config, name, manifest=manifest, report=report)

    if config.output.write_index and report.written:
        landing = _landing_filename(config, manifest)
        index = write_index(config.output_dir(), landing)
        report.written.append(index)
        logger.info("landing page: %s -> %s", index.name, landing)

    logger.info(report.summary())
    return report


def _landing_filename(config: Config, manifest: Manifest) -> str:
    """The page ``index.html`` redirects to, from ``landing:`` in the config."""
    node = config.landing.node or config.nodes.focus
    scenario = config.landing_scenario.name
    try:
        page: Page = manifest[config.landing.page]
    except KeyError:
        page = manifest.pages[0]
        logger.warning(
            "landing.page=%r is not in the built manifest; falling back to %r",
            config.landing.page,
            page.id,
        )
    if node is None:
        raise ValueError(
            "cannot determine the landing node: set either landing.node or nodes.focus"
        )
    return page.filename(node, scenario)
