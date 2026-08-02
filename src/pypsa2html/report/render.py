"""HTML assembly: figures + narrative -> pages -> a navigable site."""

from __future__ import annotations

import datetime
import html
import logging
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .. import __version__

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Pinned so a page rendered today keeps rendering the same way tomorrow.
PLOTLY_CDN = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>'


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(default=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def figure_to_html(fig, *, plotly: str = "cdn", first_on_page: bool = False) -> str:
    """Serialise a plotly figure to an embeddable fragment.

    The plotly bundle is emitted at most once per page (by the template for
    ``cdn``, by the first figure for ``inline``).  Legacy SEPIA passed
    ``include_plotlyjs='cdn'`` on every call, so a costs page loaded the 3 MB
    bundle five times.
    """
    include = False
    if plotly == "inline" and first_on_page:
        include = True
    return fig.to_html(
        full_html=False,
        include_plotlyjs=include,
        config={"displaylogo": False, "responsive": True},
    )


@dataclass
class RenderedSection:
    id: str
    title: str
    body: str
    description: str = ""

    def to_html(self) -> str:
        desc = f'<div class="p2h-desc">{self.description}</div>' if self.description else ""
        return (
            f'<section class="p2h-section" id="{html.escape(self.id, quote=True)}">'
            f"<h2>{html.escape(self.title)}</h2>{desc}"
            f'<div class="p2h-chart">{self.body}</div>'
            f"</section>"
        )


def missing_section(section, reason: str) -> RenderedSection:
    """Placeholder for a section whose inputs were unavailable.

    Legacy SEPIA raised ``NameError`` from the page-assembly code whenever a
    chart was switched off or its input file was missing.  A visible, named
    placeholder is more useful than a crash and than a silent gap.
    """
    logger.warning("section %s skipped: %s", section.id, reason)
    return RenderedSection(
        id=section.id,
        title=section.title,
        body=f'<div class="p2h-missing">Not available: {html.escape(reason)}</div>',
    )


def render_page(
    *,
    page,
    manifest,
    sections: list[RenderedSection],
    node,
    nodes,
    scenario,
    scenarios,
    project,
    plotly: str = "cdn",
    scenario_note: str = "",
    scenario_prefixes: dict[str, str] | None = None,
) -> str:
    """Render one HTML page.

    ``scenario_prefixes`` maps a scenario name to the relative path from this
    page's directory to that scenario's output directory, so the scenario
    switcher works whether all scenarios share one folder or each lives beside
    its own results tree.
    """
    prefixes = scenario_prefixes or {s.name: "" for s in scenarios}
    nav = [
        {
            "id": p.id,
            "title": p.title,
            "href": p.filename(node.code, scenario.name),
            "shared": p.shared,
        }
        for p in manifest
    ]
    template = _env().get_template("page.html.j2")
    return template.render(
        project=project,
        page={"id": page.id, "title": page.title, "shared": page.shared,
              "sections": list(page.sections)},
        manifest=nav,
        sections=[{"id": s.id, "title": s.title} for s in sections],
        main_content="\n".join(s.to_html() for s in sections),
        node=node,
        nodes=list(nodes),
        scenario=scenario,
        scenarios=[{"name": s.name, "label": s.label} for s in scenarios],
        scenario_prefixes={s.name: prefixes.get(s.name, "") for s in scenarios},
        plotly_js=PLOTLY_CDN if plotly == "cdn" else "",
        scenario_note=scenario_note,
        version=__version__,
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def write_index(out_dir: Path, target: str) -> Path:
    """Write an ``index.html`` that redirects to the landing page.

    The legacy site had no entry point at all: which page you landed on
    depended on a ``localStorage`` value and a filename convention replicated
    in three separate places.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    escaped = html.escape(target, quote=True)
    path.write_text(
        "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='0; url={escaped}'>"
        f"<link rel='canonical' href='{escaped}'>"
        "<title>Redirecting…</title></head>"
        f"<body><p>Redirecting to <a href='{escaped}'>{escaped}</a>.</p></body></html>\n",
        encoding="utf-8",
    )
    return path
