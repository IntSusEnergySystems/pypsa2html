"""Page rendering and the build loop.

These stay fast by stubbing the chart builders: the point is the assembly,
routing and navigation, not the figures.
"""

from __future__ import annotations

import pytest

from pypsa2html.build import _landing_filename, _narrative, resolve_builder
from pypsa2html.config import load_config
from pypsa2html.nodes import build_node_set
from pypsa2html.pages import load_manifest
from pypsa2html.report.render import (
    RenderedSection,
    missing_section,
    render_page,
    write_index,
)


@pytest.fixture
def node_set():
    return build_node_set(
        ["BEWAL", "DE"],
        labels={"BEWAL": "Wallonia", "DE": "Germany"},
        focus="BEWAL",
        aggregate_code="ALL",
        aggregate_label="All regions",
    )


@pytest.fixture
def scenarios(minimal_config):
    return load_config(minimal_config).scenarios


def _render(node_set, scenarios, project, page_id="emissions", plotly="cdn"):
    manifest = load_manifest()
    page = manifest[page_id]
    sections = [
        RenderedSection(id=s.id, title=s.title, body="<div>chart</div>")
        for s in page.sections
    ]
    return render_page(
        page=page,
        manifest=manifest,
        sections=sections,
        node=node_set["BEWAL"],
        nodes=node_set,
        scenario=scenarios[0],
        project=project,
        plotly=plotly,
    )


def test_page_renders_valid_shell(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    # No unsubstituted jinja tokens -- the legacy raw.html shipped four.
    assert "{{" not in html and "{%" not in html


def test_navigation_is_generated_from_the_node_set(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    for code, label in [("BEWAL", "Wallonia"), ("DE", "Germany"), ("ALL", "All regions")]:
        assert f'value="{code}"' in html
        assert label in html
    assert 'value="BEWAL" selected' in html


def test_left_nav_lists_every_page_in_the_manifest(node_set, scenarios, minimal_config):
    """A Sankey rebuild must still link to the rest of the site in the TOC."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project, page_id="sankeys")
    assert 'class="pages"' in html
    assert "Sankey diagrams" in html
    assert "Emissions" in html
    assert 'href="BEWAL_emissions_demo.html"' in html
    assert 'href="BEWAL_sankeys_demo.html"' in html


def test_build_parser_accepts_only():
    from pypsa2html.cli import build_parser

    args = build_parser().parse_args(["build", "-c", "x.yaml", "--only", "sankeys"])
    assert args.only_pages == ["sankeys"]


def test_toc_is_in_the_dom_not_a_js_string(node_set, scenarios, minimal_config):
    """Legacy injected the TOC into `secondaryTOC.innerHTML = "..."`."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    assert 'innerHTML = "' not in html
    assert '<a href="#ghg_by_sector">' in html


def test_anchors_are_unique(node_set, scenarios, minimal_config):
    """Legacy reused id='ghg' for four sections, so three TOC links were dead."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    manifest = load_manifest()
    ids = [s.id for s in manifest["emissions"].sections]
    for sid in ids:
        assert html.count(f'id="{sid}"') == 1


def test_plotly_bundle_included_once(node_set, scenarios, minimal_config):
    """Legacy emitted include_plotlyjs='cdn' per figure -- 5 loads on one page."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    assert html.count("cdn.plot.ly") == 1


def test_inline_mode_emits_no_cdn(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project, plotly="inline")
    assert "cdn.plot.ly" not in html


def test_shared_page_link_omits_the_node_prefix(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project, page_id="maps")
    assert 'href="maps_demo.html"' in html
    assert "BEWAL_maps" not in html


def test_a_shared_page_does_not_claim_a_region(node_set, scenarios, minimal_config):
    """One file serves every region, so naming one would attribute it wrongly."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project, page_id="maps")
    assert "<h1 style=\"margin-top:0\">Maps</h1>" in html
    assert "Wallonia &mdash; Maps" not in html


def test_a_per_node_page_still_names_its_region(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project, page_id="emissions")
    assert "Wallonia &mdash; Emissions" in html


def test_section_body_escapes_the_title_but_not_the_chart():
    section = RenderedSection(id="x", title="A & B <script>", body="<div>chart</div>")
    html = section.to_html()
    assert "A &amp; B &lt;script&gt;" in html
    assert "<div>chart</div>" in html


def test_missing_section_is_visible_not_fatal():
    class S:
        id, title = "x", "X"

    rendered = missing_section(S(), "input file absent")
    assert "p2h-missing" in rendered.body
    assert "input file absent" in rendered.body


# -- build helpers ---------------------------------------------------------

def test_resolve_builder_returns_none_for_missing_module():
    assert resolve_builder("nosuchmodule.nosuchfunc") is None


def test_resolve_builder_rejects_a_bare_name():
    with pytest.raises(ValueError, match="<module>.<function>"):
        resolve_builder("bare")


def test_narrative_resolves_node_then_default():
    class C:
        texts = {
            "sankey_energy": {"default": "<p>generic</p>", "BEWAL": "<p>walloon</p>"},
            "plain": "<p>string form</p>",
        }

    assert _narrative(C(), "sankey_energy", "BEWAL") == "<p>walloon</p>"
    # Legacy looked up f"{country}_{slug}" against prefix-stripped keys, so
    # every node other than BE/EU silently got nothing.
    assert _narrative(C(), "sankey_energy", "DE") == "<p>generic</p>"
    assert _narrative(C(), "plain", "DE") == "<p>string form</p>"
    assert _narrative(C(), "absent", "DE") == ""


def test_landing_filename_from_config(minimal_config):
    cfg = load_config(minimal_config)
    manifest = load_manifest(include_pages=cfg.output.pages)
    assert _landing_filename(cfg, manifest) == "AA_emissions_demo.html"


def test_landing_filename_for_another_scenario(minimal_config):
    """Each scenario's own index.html opens on that scenario's page."""
    cfg = load_config(minimal_config)
    manifest = load_manifest(include_pages=cfg.output.pages)
    assert _landing_filename(cfg, manifest, scenario="other") == "AA_emissions_other.html"


def test_landing_falls_back_when_the_page_is_disabled(minimal_config):
    cfg = load_config(minimal_config)
    manifest = load_manifest(include_pages=["sankeys"])
    # emissions was excluded; must not raise
    assert _landing_filename(cfg, manifest) == "AA_sankeys_demo.html"


def _two_scenario_config(tmp_path, output_dir):
    import yaml

    cfg = {
        "root": str(tmp_path),
        "scenarios": [
            {"name": "alpha", "results_dir": "results/alpha"},
            {"name": "beta", "results_dir": "results/beta"},
        ],
        "output": {"dir": output_dir},
    }
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return load_config(path)


def test_per_scenario_output_dir(tmp_path):
    """HTML must land beside each scenario's own csvs/graphs/networks."""
    cfg = _two_scenario_config(tmp_path, "results/{scenario}/html")
    assert cfg.output_is_per_scenario
    assert cfg.output_dir("alpha") == (tmp_path / "results/alpha/html").resolve()
    assert cfg.output_dir("beta") == (tmp_path / "results/beta/html").resolve()


def test_shared_output_dir_still_supported(tmp_path):
    cfg = _two_scenario_config(tmp_path, "results/html")
    assert not cfg.output_is_per_scenario
    assert cfg.output_dir("alpha") == cfg.output_dir("beta")


def test_per_scenario_dir_requires_a_scenario(tmp_path):
    cfg = _two_scenario_config(tmp_path, "results/{scenario}/html")
    with pytest.raises(ValueError, match="a scenario name is required"):
        cfg.output_dir()


def test_common_output_root(tmp_path):
    cfg = _two_scenario_config(tmp_path, "results/{scenario}/html")
    assert cfg.common_output_root() == (tmp_path / "results").resolve()


def test_a_scenario_page_has_no_scenario_switcher(node_set, scenarios, minimal_config):
    """A report built alone cannot know its siblings: the old switcher pointed
    at pages that were never built (pypsa-wal 1 Oct central, D24)."""
    project = load_config(minimal_config).project
    html = _render(node_set, scenarios, project)
    assert 'id="p2h-scenario"' not in html
    assert "P2H.prefix" not in html
    assert 'id="p2h-node"' in html


def test_compare_link_and_subtitle_in_the_top_bar(node_set, scenarios, minimal_config):
    project = load_config(minimal_config).project
    manifest = load_manifest()
    page = manifest["emissions"]
    html = render_page(
        page=page,
        manifest=manifest,
        sections=[RenderedSection(id="s", title="S", body="")],
        node=node_set["BEWAL"],
        nodes=node_set,
        scenario=scenarios[0],
        project=project,
        subtitle="Demo run",
        links=[{"label": "Compare scenarios", "href": "../compare/index.html"}],
    )
    assert 'href="../compare/index.html"' in html
    assert "Demo run" in html
    assert "scenario <strong>Demo</strong>" in html


def test_file_tag_names_every_link(node_set, scenarios, minimal_config):
    """The comparison renders through the same template with its own tag."""
    project = load_config(minimal_config).project
    manifest = load_manifest(site="compare")
    page = manifest["key_figures"]
    html = render_page(
        page=page,
        manifest=manifest,
        sections=[RenderedSection(id="s", title="S", body="")],
        node=node_set["BEWAL"],
        nodes=node_set,
        scenario=scenarios[0],
        project=project,
        file_tag="compare",
        footer="comparison of 2 scenarios",
    )
    assert 'href="BEWAL_overview_compare.html"' in html
    assert 'href="scenarios_compare.html"' in html
    assert "comparison of 2 scenarios" in html
    assert '"tag": "compare"' in html or "tag: \"compare\"" in html


def test_write_index_points_at_the_landing_page(tmp_path):
    path = write_index(tmp_path, "BEWAL_overview_ref.html")
    text = path.read_text()
    assert path.name == "index.html"
    assert "BEWAL_overview_ref.html" in text
    assert "http-equiv='refresh'" in text


def test_build_site_writes_pages_summary_and_index(minimal_config):
    """A scenario build ends with its summary, even when every extractor fails."""
    import json

    from pypsa2html.build import build_site

    cfg = load_config(minimal_config)
    report = build_site(cfg)
    out = cfg.output_dir("demo")
    assert (out / "index.html").exists()
    assert "AA_emissions_demo.html" in (out / "index.html").read_text()
    summary = cfg.summary_dir("demo") / "summary.json"
    assert summary in report.summaries
    meta = json.loads(summary.read_text())
    assert meta["scenario"]["name"] == "demo"
    assert meta["horizons"] == [2030, 2040]
    # One scenario: no comparison report.
    assert report.index is None
    assert not cfg.compare_dir().exists()


def test_build_site_rejects_an_unknown_scenario(minimal_config):
    from pypsa2html.build import build_site

    with pytest.raises(KeyError, match="unknown scenario"):
        build_site(load_config(minimal_config), scenarios=["typo"])


def test_cli_parses_compare():
    from pypsa2html.cli import build_parser

    args = build_parser().parse_args(
        ["compare", "-c", "x.yaml", "results/a", "results/b", "--reference", "a",
         "--recompute"]
    )
    assert [str(p) for p in args.sources] == ["results/a", "results/b"]
    assert args.reference == "a" and args.recompute
    build = build_parser().parse_args(["build", "-c", "x.yaml", "--no-compare"])
    assert build.compare is False
