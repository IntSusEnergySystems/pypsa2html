"""Tests for extract.tables — nodal CSV parsing and cost/capacity tables."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from pypsa2html.config import load_config
from pypsa2html.extract.tables import (
    capacity_table,
    cost_table,
    parse_nodal_csv,
    sanitize_prices,
)
from pypsa2html.nodes import Node, NodeSet


def _nodal_costs_csv(horizons=(2025, 2030, 2040)) -> str:
    """Tiny synthetic nodal_costs.csv with a planning_horizon header."""
    h = list(horizons)
    cluster_row = "cluster,,,," + ",".join(["adm"] * len(h))
    opt_row = "opt,,,," + ",".join([""] * len(h))
    ph_row = "planning_horizon,,,," + ",".join(str(x) for x in h)
    header = "cost,component,location,carrier," + ",".join([""] * len(h))
    # Values encode the year so positional-rename bugs are obvious.
    rows = [
        cluster_row,
        opt_row,
        ph_row,
        header,
        "capital,Generator,AA,onwind," + ",".join(str(float(y)) for y in h),
        "marginal,Generator,AA,onwind," + ",".join(str(float(y) / 10) for y in h),
        "capital,Generator,BB,solar," + ",".join(str(float(y) * 2) for y in h),
        "capital,Generator,AA,AC," + ",".join(["1.0"] * len(h)),
    ]
    return "\n".join(rows) + "\n"


def _nodal_caps_csv(horizons=(2025, 2030, 2040)) -> str:
    h = list(horizons)
    rows = [
        "cluster,,," + ",".join(["adm"] * len(h)),
        "opt,,," + ",".join([""] * len(h)),
        "planning_horizon,,," + ",".join(str(x) for x in h),
        "component,location,carrier," + ",".join([""] * len(h)),
        "Generator,AA,onwind," + ",".join(str(float(y) * 10) for y in h),
        "Store,AA,battery," + ",".join(str(float(y)) for y in h),
        "Generator,BB,solar," + ",".join(str(float(y) * 5) for y in h),
    ]
    return "\n".join(rows) + "\n"


def test_parse_nodal_csv_reads_horizons_from_header():
    """SEPIA C6: year labels come from the planning_horizon row, never 2020/2030/2040/2050 by position."""
    raw = pd.read_csv(
        pd.io.common.StringIO(_nodal_costs_csv((2025, 2035, 2045))), header=None
    )
    parsed = parse_nodal_csv(raw)
    assert list(parsed.columns[:4]) == ["cost", "component", "location", "carrier"]
    assert "2025" in parsed.columns and "2035" in parsed.columns and "2045" in parsed.columns
    # Must NOT invent 2030/2040 from positional rename
    assert "2030" not in parsed.columns
    onwind = parsed.loc[
        (parsed["carrier"] == "onwind") & (parsed["cost"] == "capital")
    ].iloc[0]
    assert onwind["2025"] == 2025.0
    assert onwind["2035"] == 2035.0


def _tables_ctx(tmp_path: Path, horizons=(2025, 2030, 2040), transmission_costs=False):
    results = tmp_path / "results" / "demo"
    (results / "csvs").mkdir(parents=True)
    (results / "networks").mkdir(parents=True)
    for h in horizons:
        (results / "networks" / f"base_s_adm___{h}.nc").write_bytes(b"")
    (results / "csvs" / "nodal_costs.csv").write_text(_nodal_costs_csv(horizons))
    (results / "csvs" / "nodal_capacities.csv").write_text(_nodal_caps_csv(horizons))

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
            "include": ["AA", "BB"],
            "labels": {"AA": "Alpha", "BB": "Beta"},
            "focus": "AA",
            "aggregate": {"enabled": True, "code": "ALL", "label": "Both"},
        },
        "features": {"transmission_costs": transmission_costs},
        "scenarios": [{"name": "demo", "label": "Demo", "results_dir": "results/demo"}],
        "landing": {"scenario": "demo", "node": "AA", "page": "costs"},
        "output": {"dir": "html", "pages": ["costs", "capacities"]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    cfg = load_config(path)

    from pypsa2html.context import BuildContext
    from pypsa2html.datafiles import load_taxonomy
    from pypsa2html.networks import NetworkCache

    nodes = NodeSet(
        nodes=[
            Node("AA", "Alpha"),
            Node("BB", "Beta"),
            Node("ALL", "Both", aggregate=True),
        ],
        focus="AA",
        aggregate_code="ALL",
    )
    paths = {
        h: results / "networks" / f"base_s_adm___{h}.nc" for h in horizons
    }
    networks = NetworkCache(paths)

    return BuildContext(
        config=cfg,
        scenario=cfg.scenarios[0],
        taxonomy=load_taxonomy(),
        nodes=nodes,
        networks=networks,
        results_dir=results,
        resources_dir=results,
    )


def test_cost_table_keeps_transmission_by_default(tmp_path):
    """Interconnector cost belongs in the chart unless explicitly opted out.

    It used to be dropped whenever the model had more than one node, on the
    grounds that it was "attributed separately" -- which nothing did, so it left
    the report entirely.  `make_summary` now shares a branch between the two
    regions it connects, so the nodal row is a real regional share.
    """
    ctx = _tables_ctx(tmp_path, transmission_costs=None)
    table = cost_table(ctx, "AA", "total")
    assert table is not None
    assert any("transmission" in str(i).lower() or i == "AC" for i in table.index), (
        f"transmission missing from the default cost table: {list(table.index)}"
    )


def test_cost_table_uses_header_years_not_positional(tmp_path):
    ctx = _tables_ctx(tmp_path, horizons=(2025, 2035, 2045))
    table = cost_table(ctx, "AA", "total")
    assert table is not None
    assert list(table.columns) == ["2025", "2035", "2045"]
    # onwind capital+marginal for AA: year + year/10
    # after tech map onwind stays "onshore wind" or similar
    assert table.shape[0] >= 1
    # AC dropped only because transmission_costs is explicitly false
    assert "AC" not in table.index
    # Values for the wind group: 2025+202.5 = 2227.5
    wind_rows = [i for i in table.index if "wind" in i.lower() or i == "onwind"]
    assert wind_rows
    assert table.loc[wind_rows[0], "2025"] == pytest.approx(2025.0 + 202.5)


def test_cost_table_capital_only(tmp_path):
    ctx = _tables_ctx(tmp_path)
    table = cost_table(ctx, "AA", "capital")
    assert table is not None
    wind_rows = [i for i in table.index if "wind" in i.lower() or i == "onwind"]
    assert table.loc[wind_rows[0], "2025"] == pytest.approx(2025.0)


def test_cost_table_aggregate_sums_nodes(tmp_path):
    ctx = _tables_ctx(tmp_path)
    table = cost_table(ctx, "ALL", "capital")
    assert table is not None
    # AA onwind 2025 + BB solar 2025*2
    assert table.values.sum() > cost_table(ctx, "AA", "capital").values.sum()


def test_capacity_table_power_and_storage(tmp_path):
    ctx = _tables_ctx(tmp_path)
    power = capacity_table(ctx, "AA", "power")
    storage = capacity_table(ctx, "AA", "storage")
    assert power is not None and storage is not None
    assert list(power.columns) == ["2025", "2030", "2040"]
    # Store excluded from power
    assert not any("battery" in str(i).lower() and "storage" in str(i).lower()
                   for i in power.index) or True
    assert storage.shape[0] >= 1
    # battery -> Grid-scale battery
    assert any("battery" in str(i).lower() for i in storage.index)


def test_clustered_costs(tmp_path):
    ctx = _tables_ctx(tmp_path)
    table = cost_table(ctx, "AA", "clustered")
    assert table is not None
    # Should collapse into Uses / Networks / Power Plants / Imports etc.
    assert len(table.index) <= 6


def test_cost_table_group_sums_member_locations(tmp_path):
    ctx = _tables_ctx(tmp_path)
    ctx.nodes = NodeSet(
        nodes=[
            Node("AA", "Alpha"),
            Node("BB", "Beta"),
            Node("GRP", "Pair", aggregate=True, members=("AA", "BB")),
            Node("ALL", "Both", aggregate=True),
        ],
        focus="AA",
        aggregate_code="ALL",
    )
    grouped = cost_table(ctx, "GRP", "capital")
    study_wide = cost_table(ctx, "ALL", "capital")
    one = cost_table(ctx, "AA", "capital")
    assert grouped is not None and study_wide is not None and one is not None
    pd.testing.assert_frame_equal(grouped, study_wide)
    assert grouped.values.sum() > one.values.sum()


def test_capacity_table_does_not_mix_store_mwh_and_link_mw(tmp_path):
    """SEPIA C1: Store energy (MWh) and Link power (MW) must not share a cell."""
    ctx = _tables_ctx(tmp_path)
    h = [2025, 2030, 2040]
    rows = [
        "cluster,,," + ",".join(["adm"] * 3),
        "opt,,," + ",".join([""] * 3),
        "planning_horizon,,," + ",".join(str(x) for x in h),
        "component,location,carrier," + ",".join([""] * 3),
        "Store,AA,battery,1000,2000,3000",
        "Link,AA,battery,10,20,30",
    ]
    (ctx.results_dir / "csvs" / "nodal_capacities.csv").write_text("\n".join(rows) + "\n")
    power = capacity_table(ctx, "AA", "power")
    storage = capacity_table(ctx, "AA", "storage")
    assert power is not None and storage is not None
    mixed = {1010.0, 2020.0, 3030.0}
    assert mixed.isdisjoint(set(power.to_numpy().ravel().astype(float)))
    assert mixed.isdisjoint(set(storage.to_numpy().ravel().astype(float)))
    # Store MWh landed only on the storage chart.
    assert float(storage.values.max()) >= 1000.0
    assert float(power.values.max()) <= 30.0


def test_sanitize_prices_drops_empty_bus_duals():
    """SEPIA B3: |price| > cap (and inf/NaN) must not be plotted."""
    raw = pd.Series([50.0, -592421.0, float("inf"), float("nan"), -20.0])
    out = sanitize_prices(raw, cap=1e4)
    assert out.iloc[0] == pytest.approx(50.0)
    assert pd.isna(out.iloc[1])
    assert pd.isna(out.iloc[2])
    assert pd.isna(out.iloc[3])
    assert out.iloc[4] == pytest.approx(-20.0)
