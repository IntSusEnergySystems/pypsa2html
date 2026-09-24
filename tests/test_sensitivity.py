"""Parameter sweeps: config wiring, generated sections and the sweep chart.

The point of the feature is that six runs differing in one number stop being
six scenarios, so most of these tests pin what a sweep point is *absent* from:
the scenario dropdown, the cross-scenario overview and the set of scenarios
pages are built for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest
import yaml

from pypsa2html.charts import sensitivity as sens
from pypsa2html.charts.base import combine_charts
from pypsa2html.config import load_config
from pypsa2html.pages import load_manifest

POINTS = (4500, 6000, 9500)


def _write(tmp_path: Path, **overrides) -> Path:
    """A config with one ordinary scenario and a three-point sweep."""
    config = {
        "root": str(tmp_path),
        "nodes": {"detect": False, "include": ["AA", "BB"], "focus": "AA"},
        "scenarios": [
            {"name": "central", "label": "Central", "results_dir": "results/central"},
            *[
                {
                    "name": f"p{v}",
                    "results_dir": f"results/p{v}",
                    "sensitivity": {"sweep": "capex", "value": v},
                }
                for v in POINTS
            ],
        ],
        "sensitivities": [
            {
                "id": "capex",
                "label": "Investment cost",
                "parameter": {"label": "Overnight cost", "unit": "EUR/kW"},
                "nodes": ["AA"],
                "metrics": [
                    {
                        "table": "capacity",
                        "kind": "power",
                        "rows": ["nuclear"],
                        "label": "Installed nuclear capacity",
                    }
                ],
            }
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


# -- config ----------------------------------------------------------------

def test_sweep_points_are_not_browsable_scenarios(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.scenario_names == ["central", "p4500", "p6000", "p9500"]
    assert cfg.report_scenario_names == ["central"]
    assert cfg.scenario("p4500").is_sweep_point


def test_sweep_points_are_ordered_by_value_not_declaration(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["scenarios"] = [raw["scenarios"][0], *reversed(raw["scenarios"][1:])]
    path.write_text(yaml.safe_dump(raw))
    cfg = load_config(path)
    assert [s.sensitivity.value for s in cfg.sweep_points("capex")] == [4500.0, 6000.0, 9500.0]


def test_point_label_defaults_to_the_value(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert [s.sensitivity.label for s in cfg.sweep_points("capex")] == ["4500", "6000", "9500"]


def test_metric_defaults_come_from_the_table(tmp_path):
    """A MW capacity table is plotted in GW without the config saying so."""
    metric = load_config(_write(tmp_path)).sensitivities[0].metrics[0]
    assert (metric.unit, metric.scale) == ("GW", 1e-3)


def test_undeclared_sweep_is_rejected(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["scenarios"][1]["sensitivity"]["sweep"] = "captex"  # typo
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="undeclared sensitivity sweep"):
        load_config(path)


def test_sweep_without_points_is_rejected(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["scenarios"] = [raw["scenarios"][0]]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="have no points"):
        load_config(path)


def test_two_points_at_the_same_value_are_rejected(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["scenarios"][2]["sensitivity"]["value"] = 4500
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="same value"):
        load_config(path)


def test_landing_on_a_sweep_point_is_rejected_with_the_reason(tmp_path):
    path = _write(tmp_path, landing={"scenario": "p4500"})
    with pytest.raises(ValueError, match="sensitivity sweep point"):
        load_config(path)


def test_a_sweep_with_no_metric_is_rejected(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["sensitivities"][0]["metrics"] = []
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="no 'metrics:'"):
        load_config(path)


def test_unknown_metric_table_is_rejected(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["sensitivities"][0]["metrics"][0]["table"] = "capacities"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="metric table must be one of"):
        load_config(path)


def test_description_is_folded_into_texts(tmp_path):
    path = _write(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["sensitivities"][0]["description"] = "<p>Read the capacity, not the cost.</p>"
    path.write_text(yaml.safe_dump(raw))
    cfg = load_config(path)
    assert cfg.texts["sensitivity_capex"]["default"].startswith("<p>Read")


def test_pinned_output_pages_gain_the_sensitivity_page(tmp_path):
    """A project that pinned output.pages before sweeps existed keeps working."""
    cfg = load_config(_write(tmp_path, output={"pages": ["overview", "costs"]}))
    assert cfg.output.pages == ["overview", "costs", "sensitivity"]


def test_output_pages_untouched_without_sweeps(tmp_path):
    path = tmp_path / "plain.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "root": str(tmp_path),
                "scenarios": [{"name": "one", "results_dir": "r"}],
                "output": {"pages": ["overview"]},
            }
        )
    )
    assert load_config(path).output.pages == ["overview"]


# -- manifest ---------------------------------------------------------------

def test_one_section_is_generated_per_sweep(tmp_path):
    cfg = load_config(_write(tmp_path))
    manifest = load_manifest(
        enable=cfg.plots,
        include_pages=cfg.output.pages,
        sensitivities=cfg.sensitivities,
    )
    page = manifest["sensitivity"]
    assert page.shared is True
    assert [s.id for s in page.sections] == ["sensitivity_capex"]
    section = page.sections[0]
    assert section.builder == "sensitivity.sweep"
    assert section.title == "Investment cost"
    assert section.params == {"sweep": "capex"}
    assert section.unit == "GW"


def test_the_page_disappears_without_sweeps():
    assert "sensitivity" not in load_manifest().ids


def test_a_sweep_section_can_be_switched_off(tmp_path):
    cfg = load_config(_write(tmp_path, plots={"sensitivity_capex": False}))
    manifest = load_manifest(
        enable=cfg.plots,
        include_pages=cfg.output.pages,
        sensitivities=cfg.sensitivities,
    )
    assert "sensitivity" not in manifest.ids


def test_a_sweep_id_is_a_valid_plots_key(tmp_path):
    """`plots:` validation must know the generated ids, or it rejects them."""
    cfg = load_config(_write(tmp_path, plots={"sensitivity_capex": True}))
    load_manifest(enable=cfg.plots, sensitivities=cfg.sensitivities)


# -- the chart --------------------------------------------------------------

@dataclass
class _Point:
    name: str
    sensitivity: object


@dataclass
class _Sctx:
    """Just enough context for the metric readers."""

    scenario: object
    table: pd.DataFrame | None
    year_columns: list[str] = field(default_factory=lambda: ["2030", "2050"])


def _contexts(tables: dict[int, pd.DataFrame | None]):
    out = []
    for value, table in tables.items():
        marker = type("S", (), {"value": float(value), "label": str(value)})()
        point = _Point(name=f"p{value}", sensitivity=marker)
        out.append((point, _Sctx(scenario=point, table=table)))
    return out


@pytest.fixture
def capacity_stub(monkeypatch):
    monkeypatch.setattr(sens, "capacity_table", lambda sctx, node, kind: sctx.table)


def _metric(**kw):
    from pypsa2html.config import SensitivityMetric

    return SensitivityMetric(**{"table": "capacity", "kind": "power", **kw})


def _table(nuclear, solar=0.0):
    return pd.DataFrame(
        {"2030": [nuclear[0], solar], "2050": [nuclear[1], solar]},
        index=["nuclear", "solar"],
    )


def test_sweep_frame_is_value_by_horizon(capacity_stub):
    contexts = _contexts(
        {9500: _table((1030, 1030)), 4500: _table((1030, 3000)), 6000: _table((1030, 1030))}
    )
    frame = sens._sweep_frame(contexts, "AA", _metric(rows=["nuclear"]))
    assert list(frame.index) == [4500.0, 6000.0, 9500.0]   # ordered, not as given
    assert list(frame.columns) == ["2030", "2050"]
    assert frame.loc[4500.0, "2050"] == pytest.approx(3.0)  # MW -> GW
    assert frame.loc[9500.0, "2050"] == pytest.approx(1.03)


def test_a_row_the_run_did_not_build_reads_zero_not_missing(capacity_stub):
    """`capacity_table` drops all-zero rows, and zero is a point on the curve."""
    built = _table((1030, 3000))
    none_built = built.drop(index="nuclear")
    frame = sens._sweep_frame(
        _contexts({4500: built, 9500: none_built}), "AA", _metric(rows=["nuclear"])
    )
    assert frame.loc[9500.0, "2050"] == 0.0
    assert not frame.isna().to_numpy().any()


def test_a_point_with_no_table_breaks_the_line_instead_of_reading_zero(capacity_stub):
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 3000)), 9500: None}), "AA", _metric(rows=["nuclear"])
    )
    assert list(frame.index) == [4500.0]


def test_rows_are_matched_case_insensitively_and_summed(capacity_stub):
    frame = sens._sweep_frame(
        _contexts({4500: _table((1000, 2000), solar=500.0)}),
        "AA",
        _metric(rows=["Nuclear", "SOLAR"]),
    )
    assert frame.loc[4500.0, "2050"] == pytest.approx(2.5)


def test_no_rows_sums_the_whole_table(capacity_stub):
    frame = sens._sweep_frame(
        _contexts({4500: _table((1000, 2000), solar=500.0)}), "AA", _metric()
    )
    assert frame.loc[4500.0, "2030"] == pytest.approx(1.5)


def test_versus_parameter_draws_one_line_per_horizon(tmp_path, capacity_stub):
    cfg = load_config(_write(tmp_path))
    spec = cfg.sensitivities[0]
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 3000)), 9500: _table((1030, 1030))}),
        "AA",
        spec.metrics[0],
    )
    fig = sens._versus_parameter(frame, spec=spec, metric=spec.metrics[0], caption="c")
    assert [t.name for t in fig.data] == ["2030", "2050"]
    assert list(fig.data[0].x) == [4500.0, 9500.0]
    assert fig.layout.xaxis.title.text == "Overnight cost [EUR/kW]"
    assert fig.layout.yaxis.title.text == "GW"


def test_over_time_is_the_same_numbers_transposed(tmp_path, capacity_stub):
    cfg = load_config(_write(tmp_path))
    spec = cfg.sensitivities[0]
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 3000)), 9500: _table((1030, 1030))}),
        "AA",
        spec.metrics[0],
    )
    fig = sens._over_time(frame, spec=spec, metric=spec.metrics[0], caption="c")
    assert [t.name for t in fig.data] == ["4500 EUR/kW", "9500 EUR/kW"]
    assert list(fig.data[0].x) == ["2030", "2050"]
    assert list(fig.data[0].y) == pytest.approx([1.03, 3.0])


def test_coincident_series_stay_tellable_apart(tmp_path, capacity_stub):
    """Colour alone hides a line that lies exactly under another one.

    Every horizon before the technology turns competitive returns the same
    floor, so the lines coincide and the reader cannot tell equal from absent.
    """
    cfg = load_config(_write(tmp_path))
    spec = cfg.sensitivities[0]
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 1030)), 9500: _table((1030, 1030))}),
        "AA",
        spec.metrics[0],
    )
    fig = sens._versus_parameter(frame, spec=spec, metric=spec.metrics[0], caption="c")
    assert len({t.line.dash for t in fig.data}) == len(fig.data)
    assert len({t.marker.symbol for t in fig.data}) == len(fig.data)


def test_the_horizon_view_keeps_its_own_axis_in_the_merged_figure(tmp_path, capacity_stub):
    """The dropdown swaps traces, so it has to swap the axes they are read on.

    Without it the second view inherited the first's x title and tick values,
    which match none of its categories -- so it drew no x ticks at all.
    """
    cfg = load_config(_write(tmp_path))
    spec = cfg.sensitivities[0]
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 3000)), 9500: _table((1030, 1030))}),
        "AA",
        spec.metrics[0],
    )
    merged = combine_charts(
        [
            (sens.VIEW_PARAMETER, sens._versus_parameter(
                frame, spec=spec, metric=spec.metrics[0], caption="c")),
            (sens.VIEW_HORIZON, sens._over_time(
                frame, spec=spec, metric=spec.metrics[0], caption="c")),
        ],
        title="Installed nuclear capacity",
        menu_title="View",
    )
    horizon = merged.layout.updatemenus[0].buttons[1].args[1]
    assert horizon["xaxis.title.text"] == "Planning horizon"
    assert horizon["xaxis.tickvals"] == ["2030", "2050"]


def test_the_title_names_the_chart_and_the_menu_names_the_view(tmp_path, capacity_stub):
    """Appending the button label to the title printed it twice, side by side."""
    cfg = load_config(_write(tmp_path))
    spec = cfg.sensitivities[0]
    frame = sens._sweep_frame(
        _contexts({4500: _table((1030, 3000)), 9500: _table((1030, 1030))}),
        "AA",
        spec.metrics[0],
    )
    merged = combine_charts(
        [
            (sens.VIEW_PARAMETER, sens._versus_parameter(
                frame, spec=spec, metric=spec.metrics[0], caption="c")),
            (sens.VIEW_HORIZON, sens._over_time(
                frame, spec=spec, metric=spec.metrics[0], caption="c")),
        ],
        title="Installed nuclear capacity",
        menu_title="View",
    )
    assert merged.layout.title.text == "Installed nuclear capacity"
    assert all("title" not in b.args[1] for b in merged.layout.updatemenus[0].buttons)
    # The caption has to clear the strip the title and the menu sit in.
    caption = merged.layout.annotations[0]
    assert caption.y < merged.layout.updatemenus[0].y
    assert merged.layout.margin.t >= 100


def test_an_unsolved_sweep_renders_a_note_not_an_empty_section(tmp_path, monkeypatch):
    """A dropped section leaves the nav entry every other page already carries."""
    cfg = load_config(_write(tmp_path))
    manifest = load_manifest(sensitivities=cfg.sensitivities)
    section = manifest["sensitivity"].sections[0]
    ctx = type("Ctx", (), {"config": cfg, "_files": {}})()
    monkeypatch.setattr(sens, "_point_contexts", lambda *a, **k: [])
    result = sens.sweep(ctx, "AA", section)
    assert result is not None
    assert "No solved results" in result.__html__()


# -- the build loop ---------------------------------------------------------

def test_the_scenario_switcher_never_offers_a_sweep_point(tmp_path):
    from pypsa2html.build import _scenario_prefixes

    cfg = load_config(_write(tmp_path, output={"dir": "results/{scenario}/html"}))
    assert set(_scenario_prefixes(cfg, "central")) == {"central"}


def test_sweep_directories_do_not_raise_the_site_root(tmp_path):
    """Points live beside the scenarios, so index.html must not move up."""
    cfg = load_config(
        _write(
            tmp_path,
            output={"dir": "results/walloon/{scenario}/html"},
            scenarios=[
                {"name": "central", "results_dir": "results/walloon/central"},
                {
                    "name": "p1",
                    "results_dir": "results/sweeps/p1",
                    "sensitivity": {"sweep": "capex", "value": 1},
                },
            ],
        )
    )
    assert cfg.common_output_root() == (tmp_path / "results/walloon/central").resolve()
