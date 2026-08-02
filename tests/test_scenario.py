"""Fast unit tests for multi-scenario helpers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from pypsa2html.charts.scenario import scenario_contexts
from pypsa2html.config import load_config
from pypsa2html.context import BuildContext
from pypsa2html.datafiles import load_taxonomy
from pypsa2html.networks import NetworkCache
from pypsa2html.nodes import Node, NodeSet
from tests.test_tables import _nodal_caps_csv, _nodal_costs_csv


def _parent_ctx(tmp_path: Path, horizons=(2025, 2030)) -> BuildContext:
    """Landing-scenario context pointing at the one present results tree."""
    present = tmp_path / "results" / "present"
    (present / "csvs").mkdir(parents=True)
    (present / "networks").mkdir(parents=True)
    for h in horizons:
        (present / "networks" / f"base_s_adm___{h}.nc").write_bytes(b"")
    (present / "csvs" / "nodal_costs.csv").write_text(_nodal_costs_csv(horizons))
    (present / "csvs" / "nodal_capacities.csv").write_text(_nodal_caps_csv(horizons))

    # Missing scenario dirs are intentionally not created.
    config = {
        "root": str(tmp_path),
        "project": {"name": "Demo"},
        "model": {
            "clusters": "adm",
            "opts": "",
            "sector_opts": "",
            "planning_horizons": list(horizons),
        },
        "nodes": {
            "detect": False,
            "include": ["AA"],
            "labels": {"AA": "Alpha"},
            "focus": "AA",
            "aggregate": {"enabled": False},
        },
        "features": {"transmission_costs": False},
        "scenarios": [
            {"name": "missing_a", "label": "Missing A", "results_dir": "results/missing_a"},
            {"name": "missing_b", "label": "Missing B", "results_dir": "results/missing_b"},
            {"name": "present", "label": "Present", "results_dir": "results/present"},
        ],
        "landing": {"scenario": "present", "node": "AA", "page": "overview"},
        "output": {"dir": "html", "pages": ["overview"]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    cfg = load_config(path)

    nodes = NodeSet(nodes=[Node("AA", "Alpha")], focus="AA")
    paths = {h: present / "networks" / f"base_s_adm___{h}.nc" for h in horizons}
    return BuildContext(
        config=cfg,
        scenario=cfg.scenario("present"),
        taxonomy=load_taxonomy(),
        nodes=nodes,
        networks=NetworkCache(paths),
        results_dir=present,
        resources_dir=present,
    )


def test_scenario_contexts_skips_missing_and_loads_present(tmp_path, caplog):
    ctx = _parent_ctx(tmp_path)
    with caplog.at_level(logging.WARNING):
        contexts = scenario_contexts(ctx)

    assert set(contexts) == {"Present"}
    assert contexts["Present"].scenario.name == "present"
    assert (contexts["Present"].results_dir / "csvs" / "nodal_costs.csv").exists()

    # Cached on second call.
    assert scenario_contexts(ctx) is contexts

    warnings = " ".join(r.message for r in caplog.records)
    assert "Missing A" in warnings
    assert "Missing B" in warnings
    assert "missing" in warnings.lower()
