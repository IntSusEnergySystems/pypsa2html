"""Summary files: what a scenario build writes for the comparison to read.

The contract is that a figure read back from ``summary.csv`` is the figure the
scenario report computed -- so the main test is a write -> read round trip
against the live tables, to full precision.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from pypsa2html.config import load_config
from pypsa2html.metrics import SYSTEM, load_kpis
from pypsa2html.nodes import build_node_set
from pypsa2html.summary import (
    INCOMPATIBLE,
    MISSING,
    OK,
    SETTINGS_DIFFER,
    STALE,
    UNVERIFIED,
    SummaryScenarioData,
    check_summary,
    read_meta,
    settings_from_config,
    summary_tables,
    write_summary,
)
from tests.test_metrics import FakeData

YEARS = ["2030", "2050"]


def make_config(tmp_path: Path, **extra) -> object:
    raw = {
        "root": str(tmp_path),
        "project": {"name": "Demo"},
        "nodes": {"detect": False, "include": ["AA", "BB"], "focus": "AA",
                  "aggregate": {"enabled": True, "code": "ALL", "label": "Both"}},
        "scenarios": [
            {"name": "a", "label": "Alpha", "results_dir": "results/a"},
            {"name": "b", "label": "Beta", "results_dir": "results/b"},
        ],
        "output": {"dir": "results/{scenario}/html"},
    }
    raw.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def make_tree(tmp_path: Path, name: str, years=(2030, 2050)) -> Path:
    results = tmp_path / "results" / name
    (results / "networks").mkdir(parents=True, exist_ok=True)
    (results / "csvs").mkdir(exist_ok=True)
    for year in years:
        (results / "networks" / f"base_s_adm___{year}.nc").write_bytes(b"x")
    (results / "csvs" / "nodal_costs.csv").write_text("a,b\n1,2\n")
    return results


def fake_ctx(config, name: str, results: Path, years=(2030, 2050)):
    nodes = build_node_set(["AA", "BB"], labels={"AA": "Alpha node"}, focus="AA",
                           aggregate_code="ALL", aggregate_label="Both")
    paths = {y: results / "networks" / f"base_s_adm___{y}.nc" for y in years}
    files = {("csv", str(results / "csvs" / "nodal_costs.csv"), "[]"): None}
    year_columns = [str(y) for y in years]

    def horizon_weights():
        gaps = [years[i + 1] - years[i] for i in range(len(years) - 1)] or [1]
        return pd.Series(gaps + [gaps[-1]], index=list(years), dtype=float)[: len(years)]

    return SimpleNamespace(
        config=config,
        scenario=config.scenario(name),
        results_dir=results,
        resources_dir=results,
        networks=SimpleNamespace(paths=paths),
        horizons=list(years),
        year_columns=year_columns,
        horizon_weights=horizon_weights,
        nodes=nodes,
        _files=files,
    )


def fake_data(scale: float = 1.0, years=YEARS) -> FakeData:
    """Tables of a scenario; ``scale`` makes a second scenario differ."""
    cols = list(years)

    def frame(rows: dict) -> pd.DataFrame:
        return pd.DataFrame({y: [v[i] for v in rows.values()] for i, y in enumerate(cols)},
                            index=list(rows))

    tables = {
        ("AA", "capacity.power"): frame({"nuclear": [1000.0 * scale, 3000.0 * scale],
                                         "solar PV (ground)": [500.0, 1500.0],
                                         "electrolysis": [0.0, 0.0]}),
        ("AA", "capacity.storage"): frame({"H2 Store": [0.0, 0.0]}),
        ("AA", "cost.total"): frame({"nuclear": [5.8e11 * scale + 0.123, 6.1e11],
                                     "solar": [1.0e9, -2.5e8]}),
        ("AA", "indicator.ghg_sector_cum"): frame({"ind_ghg": [10.0, 30.0 * scale]}),
        ("AA", "self_sufficiency.ratio"): frame({"primary": [20.0, 45.0 * scale],
                                                 "electricity": [101.0, np.nan]}),
        ("AA", "price.electricity"): frame({"mean": [104.2, 113.6 * scale],
                                            "p95": [np.nan, 286.7]}),
        ("AA", "flow.co2"): frame({"pro_ghg>stm>": [1.59, 4.84 * scale],
                                   "gas_ghg>stm>ccgtcc": [0.0, 0.0]}),
        ("BB", "capacity.power"): frame({"nuclear": [0.0, 500.0]}),
        (SYSTEM, "global.objective"): frame({"objective": [3.427720e11 * scale, 2.906002e11]}),
        (SYSTEM, "global.constraints"): frame({"CO2Limit": [-97.98, -660.70 * scale]}),
    }
    return FakeData(tables=tables, year_columns=cols)


def write(tmp_path, config, name="a", scale=1.0, kpis=None):
    results = make_tree(tmp_path, name)
    ctx = fake_ctx(config, name, results)
    out = config.summary_dir(name)
    kpis = kpis if kpis is not None else config.kpis
    return write_summary(ctx, out, kpis, data=fake_data(scale)), results


# -- round trip ------------------------------------------------------------------

def test_round_trip_matches_the_live_tables(tmp_path):
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    assert path.name == "summary.json"
    assert (path.parent / "summary.csv").exists()
    data = SummaryScenarioData.load(path)
    live = fake_data()
    for (node, key), expected in live.tables.items():
        got = data.table(node, key)
        assert got is not None, (node, key)
        family = key.partition(".")[0]
        expected = expected.copy()
        if family in ("capacity", "cost", "indicator", "flow"):
            # zero rows are not written; absent rows read zero
            expected = expected.loc[(expected.fillna(0).abs() > 0).any(axis=1)]
            got = got.reindex(expected.index)
        got = got.reindex(index=expected.index, columns=expected.columns)
        np.testing.assert_allclose(got.to_numpy(dtype=float), expected.to_numpy(dtype=float),
                                   rtol=1e-9, equal_nan=True)


def test_full_precision_survives(tmp_path):
    """An objective difference of 60 MEUR on 2.9e11 must not round away."""
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    data = SummaryScenarioData.load(path)
    cost = data.table("AA", "cost.total")
    assert cost.loc["nuclear", "2030"] == pytest.approx(5.8e11 + 0.123, rel=1e-12, abs=1e-3)


def test_an_all_zero_table_is_present_and_empty(tmp_path):
    """Present-but-empty must not read as missing: zero storage is a value."""
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    data = SummaryScenarioData.load(path)
    storage = data.table("AA", "capacity.storage")
    assert storage is not None and storage.empty
    assert data.table("BB", "capacity.storage") is None   # never computed


def test_missing_values_stay_missing_for_prices(tmp_path):
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    prices = SummaryScenarioData.load(path).table("AA", "price.electricity")
    assert np.isnan(prices.loc["p95", "2030"])
    assert prices.loc["p95", "2050"] == pytest.approx(286.7)


def test_kpis_are_stored_with_their_digest(tmp_path):
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    data = SummaryScenarioData.load(path)
    kpis = {k.id: k for k in config.kpis}
    stored = data.stored_kpi("AA", kpis["cap_nuclear"])
    assert list(stored) == pytest.approx([1.0, 3.0])
    objective = data.stored_kpi(SYSTEM, kpis["objective"])
    assert objective["2030"] == pytest.approx(342772.0)
    # A changed definition is not served from the summary.
    changed = load_kpis([{"id": "cap_nuclear", "rows": ["solar PV (ground)"]}])
    changed = next(k for k in changed if k.id == "cap_nuclear")
    assert data.stored_kpi("AA", changed) is None


def test_meta_records_provenance(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    meta = json.loads(path.read_text())
    assert meta["schema_version"] == 1
    assert meta["scenario"]["name"] == "a" and meta["scenario"]["label"] == "Alpha"
    assert meta["year_columns"] == YEARS
    assert [n["code"] for n in meta["nodes"]] == ["AA", "BB", "ALL"]
    assert "networks/base_s_adm___2030.nc" in meta["inputs"]["results"]
    assert "csvs/nodal_costs.csv" in meta["inputs"]["results"]
    assert meta["settings"]["features.nuclear_primary"] == "uranium"
    assert meta["solved_at"]
    assert "capacity.power" in meta["tables"]["AA"]


def test_a_failing_table_is_logged_not_raised(tmp_path):
    config = make_config(tmp_path)
    results = make_tree(tmp_path, "a")
    ctx = fake_ctx(config, "a", results)
    data = fake_data()

    original = data.table

    def broken(node, key):
        if key == "cost.total":
            raise RuntimeError("boom")
        return original(node, key)

    data.table = broken
    path = write_summary(ctx, config.summary_dir("a"), config.kpis, data=data)
    meta = json.loads(path.read_text())
    assert any("boom" in e for e in meta["errors"])


def test_locations_for_groups_and_the_aggregate(tmp_path):
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    data = SummaryScenarioData.load(path)
    assert data.locations_for("AA") == ["AA"]
    assert data.locations_for("ALL") is None


# -- which tables ------------------------------------------------------------------

def test_tables_referenced_by_kpis_and_sweeps_are_added(tmp_path):
    config = make_config(tmp_path)
    kpis = load_kpis(
        [{"id": "h2", "source": "balance", "kind": "H2", "rows": ["supply:*"]},
         {"id": "gc", "source": "global", "kind": "constraints", "rows": ["x"]},
         {"id": "r", "source": "ratio", "numerator": {"source": "price", "kind": "gas",
                                                       "rows": ["mean"]},
          "denominator": "h2"}],
        defaults=False,
    )
    node_keys, system_keys = summary_tables(config, kpis)
    assert "balance.H2" in node_keys and "price.gas" in node_keys
    assert "global.constraints" in system_keys
    assert "flow.co2" in node_keys


def test_flows_can_be_left_out(tmp_path):
    config = make_config(tmp_path, summary={"flows": False})
    node_keys, _ = summary_tables(config, [])
    assert "flow.co2" not in node_keys


# -- freshness ------------------------------------------------------------------------

def test_a_fresh_summary_is_ok(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    assert check_summary(read_meta(path), results, config).status == OK


def test_a_changed_network_makes_it_stale(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    target = results / "networks" / "base_s_adm___2050.nc"
    target.write_bytes(b"resolved again")
    later = time.time() + 5
    os.utime(target, (later, later))
    status = check_summary(read_meta(path), results, config)
    assert status.status == STALE
    assert any("base_s_adm___2050.nc" in r for r in status.reasons)


def test_a_new_horizon_makes_it_stale(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    extra = results / "networks" / "base_s_adm___2040.nc"
    extra.write_bytes(b"x")
    later = time.time() + 5
    os.utime(extra, (later, later))
    assert check_summary(read_meta(path), results, config).status == STALE


def test_an_unreachable_tree_is_unverified(tmp_path):
    config = make_config(tmp_path)
    path, _ = write(tmp_path, config)
    status = check_summary(read_meta(path), tmp_path / "elsewhere", config)
    assert status.status == UNVERIFIED


def test_another_schema_is_incompatible(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    meta = read_meta(path)
    meta["schema_version"] = 99
    assert check_summary(meta, results, config).status == INCOMPATIBLE


def test_no_summary_is_missing():
    assert check_summary(None, None).status == MISSING


def test_other_settings_are_reported(tmp_path):
    config = make_config(tmp_path)
    path, results = write(tmp_path, config)
    (tmp_path / "x").mkdir()
    other = make_config(tmp_path / "x", features={"nuclear_primary": "electricity"})
    status = check_summary(read_meta(path), results, other)
    assert status.status == SETTINGS_DIFFER
    assert "features.nuclear_primary" in status.reasons[0]
    assert settings_from_config(other)["features.nuclear_primary"] == "electricity"


def test_shared_output_dir_keeps_summaries_apart(tmp_path):
    config = make_config(tmp_path, output={"dir": "html"})
    assert config.summary_dir("a") != config.summary_dir("b")
    assert config.summary_dir("a").parent == config.output_dir() / "summaries"
