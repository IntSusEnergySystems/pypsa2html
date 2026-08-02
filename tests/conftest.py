"""Shared fixtures.

The suite is deliberately fast: nothing here loads a solved network or renders
a full report. Tests that need a real model are marked ``needs_model`` and skip
when the model directory is absent.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

DATA = Path(__file__).parent / "data" / "negawatt-ref"

#: Real models, present on the developer's machine but not in the repository.
MODEL_ROOTS = {
    "negawatt": Path("/home/sylvain/svn/pypsa-eur_negawatt"),
    "pypsa-wal": Path("/home/sylvain/svn/pypsa-wal"),
}


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def year_columns() -> list[str]:
    return ["2020", "2030", "2040", "2050"]


@pytest.fixture(scope="session")
def energy_flows_be() -> pd.DataFrame:
    """Real négaWatt energy flow table for Belgium."""
    return pd.read_csv(DATA / "flows" / "BE_energy.csv")


@pytest.fixture(scope="session")
def carbon_flows_be() -> pd.DataFrame:
    """Real négaWatt carbon flow table for Belgium."""
    return pd.read_csv(DATA / "flows" / "BE_carbon.csv")


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """A valid config pointing at a fake results tree with two horizons.

    Enough to exercise config loading, node handling and page assembly without
    touching a real model.
    """
    results = tmp_path / "results" / "demo"
    (results / "networks").mkdir(parents=True)
    for horizon in (2030, 2040):
        (results / "networks" / f"base_s_adm___{horizon}.nc").write_bytes(b"")

    config = {
        "root": str(tmp_path),
        "project": {"name": "Demo"},
        "model": {"clusters": "adm", "opts": "", "sector_opts": ""},
        "nodes": {
            "detect": False,
            "include": ["AA", "BB"],
            "labels": {"AA": "Alpha", "BB": "Beta"},
            "focus": "AA",
            "aggregate": {"enabled": True, "code": "ALL", "label": "Both"},
        },
        "scenarios": [{"name": "demo", "label": "Demo", "results_dir": "results/demo"}],
        "landing": {"scenario": "demo", "node": "AA", "page": "emissions"},
        "output": {"dir": "html", "pages": ["emissions", "sankeys"]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def requires_model(name: str):
    """Skip marker for tests needing a real solved model on disk."""
    root = MODEL_ROOTS[name]
    return pytest.mark.skipif(
        not (root / "results").exists(),
        reason=f"model {name} not available at {root}",
    )
