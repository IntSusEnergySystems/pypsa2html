"""The comparison report, built from summary files alone.

Two fake scenarios are summarised (no networks exist), then compared.  The
networks module is booby-trapped so a test fails if the comparison tries to
load a solved network.
"""

from __future__ import annotations

import logging
import shutil

import pandas as pd
import pytest

from pypsa2html.compare import CompareContext, compare_site, resolve_sources
from pypsa2html.datafiles import load_taxonomy
from pypsa2html.nodes import build_node_set
from tests.test_metrics import FakeData
from tests.test_summary import make_config, write


@pytest.fixture
def no_networks(monkeypatch):
    import pypsa2html.networks as networks

    def refuse(path):
        raise AssertionError(f"the comparison loaded a network: {path}")

    monkeypatch.setattr(networks, "_load_network", refuse)


@pytest.fixture
def two_summaries(tmp_path):
    config = make_config(tmp_path)
    write(tmp_path, config, "a", scale=1.0)
    write(tmp_path, config, "b", scale=2.0)
    return config, tmp_path


def test_compare_writes_pages_csv_and_index(two_summaries, no_networks):
    config, tmp_path = two_summaries
    out = tmp_path / "cmp"
    report = compare_site(config, out_dir=out)
    names = {p.name for p in out.iterdir()}
    for expected in ("AA_key_figures_compare.html", "BB_key_figures_compare.html",
                     "AA_overview_compare.html", "scenarios_compare.html",
                     "comparison.csv", "index.html"):
        assert expected in names, expected
    assert report.index == out / "index.html"
    assert "AA_key_figures_compare.html" in report.index.read_text()


def test_differences_are_against_the_reference(two_summaries, no_networks):
    config, tmp_path = two_summaries
    out = tmp_path / "cmp"
    compare_site(config, out_dir=out)
    table = pd.read_csv(out / "comparison.csv")
    row = table.query("scenario == 'b' and node == 'AA' and kpi == 'cap_nuclear' and year == 2050")
    assert row["value"].item() == pytest.approx(6.0)
    assert row["reference"].item() == pytest.approx(3.0)
    assert row["delta"].item() == pytest.approx(3.0)
    assert row["delta_pct"].item() == pytest.approx(100.0)
    objective = table.query("scenario == 'b' and kpi == 'objective' and year == 2030")
    assert objective["node"].item() == "_system"
    assert objective["delta"].item() == pytest.approx(342772.0)


def test_another_reference(two_summaries, no_networks):
    config, tmp_path = two_summaries
    out = tmp_path / "cmp"
    compare_site(config, out_dir=out, reference="b")
    table = pd.read_csv(out / "comparison.csv")
    row = table.query("scenario == 'a' and node == 'AA' and kpi == 'cap_nuclear' and year == 2050")
    assert row["delta"].item() == pytest.approx(-3.0)


def test_key_figures_page(two_summaries, no_networks):
    config, tmp_path = two_summaries
    (tmp_path / "results" / "a" / "html" / "AA_key_figures_a.html").write_text("page")
    out = tmp_path / "cmp"
    compare_site(config, out_dir=out)
    html = (out / "AA_key_figures_compare.html").read_text()
    assert "Alpha" in html and "Beta" in html
    assert "data-kf=" in html
    assert "Difference to Alpha" in html
    assert "reference" in html
    # The column header links into the scenario's own report.
    assert 'href="../results/a/html/AA_key_figures_a.html"' in html
    # A scenario report never gets a scenario dropdown; the comparison has none either.
    assert 'id="p2h-scenario"' not in html


def test_the_comparison_charts_carry_a_difference_view(two_summaries, no_networks):
    config, tmp_path = two_summaries
    out = tmp_path / "cmp"
    compare_site(config, out_dir=out)
    html = (out / "AA_overview_compare.html").read_text()
    assert "Beta \\u2212 Alpha" in html or "Beta − Alpha" in html
    assert "Difference to Alpha" in html


def test_sources_by_path(two_summaries, no_networks):
    config, tmp_path = two_summaries
    sources = resolve_sources(
        config,
        [tmp_path / "results" / "a",                      # results tree
         tmp_path / "results" / "b" / "html"],             # HTML folder
    )
    assert [s.name for s in sources] == ["a", "b"]
    assert [s.label for s in sources] == ["Alpha", "Beta"]   # the config's labels


def test_a_copied_summary_compares_without_its_tree(two_summaries, no_networks):
    """Trees solved elsewhere: copy the HTML folder, compare from it."""
    config, tmp_path = two_summaries
    copy = tmp_path / "copied" / "b_html"
    shutil.copytree(tmp_path / "results" / "b" / "html", copy)
    shutil.rmtree(tmp_path / "results" / "b")
    out = tmp_path / "cmp"
    compare_site(config, [tmp_path / "results" / "a", copy], out_dir=out)
    page = (out / "scenarios_compare.html").read_text()
    assert "unverified" in page
    table = pd.read_csv(out / "comparison.csv")
    assert set(table["scenario"]) == {"a", "b"}


def test_a_missing_summary_is_skipped_with_a_warning(tmp_path, no_networks, caplog):
    config = make_config(tmp_path)
    write(tmp_path, config, "a")
    (tmp_path / "results" / "b").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        compare_site(config, out_dir=tmp_path / "cmp")
    assert "b: missing summary" in caplog.text
    page = (tmp_path / "cmp" / "scenarios_compare.html").read_text()
    assert "is not compared" in page


def test_on_stale_fail_raises(tmp_path, no_networks):
    config = make_config(tmp_path, compare={"on_stale": "fail"})
    write(tmp_path, config, "a")
    with pytest.raises(FileNotFoundError, match="summarize"):
        compare_site(config, out_dir=tmp_path / "cmp")


def test_mixed_vintages_raise_a_banner(two_summaries, no_networks):
    import json

    config, tmp_path = two_summaries
    path = tmp_path / "results" / "b" / "html" / "summary.json"
    meta = json.loads(path.read_text())
    meta["solved_at"] = "2026-09-15T08:00:00"
    path.write_text(json.dumps(meta))
    out = tmp_path / "cmp"
    compare_site(config, out_dir=out)
    page = (out / "AA_key_figures_compare.html").read_text()
    assert "Read the differences with care" in page
    assert "days apart" in page


def test_nothing_to_compare_is_an_error(tmp_path, no_networks):
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="no compared scenario has a usable summary"):
        compare_site(config, out_dir=tmp_path / "cmp")


def test_union_of_years_and_nodes(tmp_path):
    config = make_config(tmp_path)
    a = FakeData(year_columns=["2030", "2050"], name="a", label="A")
    b = FakeData(year_columns=["2030", "2040", "2050"], name="b", label="B")
    ctx = CompareContext(
        config=config, taxonomy=load_taxonomy(), scenarios={"A": a, "B": b},
        sources=[], reference="A",
        nodes=build_node_set(["AA"], focus="AA"), out_dir=tmp_path, kpis=[],
    )
    assert ctx.year_columns == ["2030", "2040", "2050"]


def test_build_site_builds_the_comparison_for_several_scenarios(tmp_path, monkeypatch):
    """`build` of two scenarios ends with the comparison, read from summaries."""
    import pypsa2html.build as build

    config = make_config(tmp_path)

    def fake_build(cfg, name, **kwargs):
        write(tmp_path, cfg, name, scale=1.0 if name == "a" else 2.0)
        return kwargs["report"]

    monkeypatch.setattr(build, "build_scenario", fake_build)
    report = build.build_site(config)
    assert report.index is None or report.index.exists()
    compare_index = config.compare_dir() / "index.html"
    assert compare_index.exists()
    root_index = config.common_output_root() / "index.html"
    assert "compare/" in root_index.read_text()


def test_build_site_of_one_scenario_builds_no_comparison(tmp_path, monkeypatch):
    import pypsa2html.build as build

    config = make_config(tmp_path)

    def fake_build(cfg, name, **kwargs):
        write(tmp_path, cfg, name)
        return kwargs["report"]

    monkeypatch.setattr(build, "build_scenario", fake_build)
    build.build_site(config, scenarios=["a"])
    assert not config.compare_dir().exists()
    # and no root index clobbering whatever lives above the scenario folder
    assert not (config.common_output_root() / "index.html").exists()


def test_number_formatting():
    from pypsa2html.charts.keyfigures import fmt

    assert fmt(290600.2) == "290 600"
    assert fmt(0.0) == "0"
    assert fmt(0.0004) == "0"
    assert fmt(0.43) == "0.43"
    assert fmt(12.345) == "12.3"
    assert fmt(3.0, signed=True) == "+3.00"
    assert fmt(-2.5, 1, signed=True) == "-2.5"
    assert fmt(float("nan")) == "–" and fmt(None) == "–"
