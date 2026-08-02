"""Shared fixtures.

The suite is deliberately fast: nothing here loads a solved network or renders
a full report. Tests that need a real model are marked ``needs_model`` and skip
when the model directory is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest
import yaml

from pypsa2html.datafiles import load_taxonomy
from pypsa2html.nodes import build_node_set

DATA = Path(__file__).parent / "data" / "negawatt-ref"

#: Legacy reference workbooks shipped with the négaWatt results tree.
CHARTDATA_ROOT = Path("/home/sylvain/svn/pypsa-eur_negawatt/results/ref/htmls")

#: Sheet indices in ChartData_*.xlsx for the GHG indicator charts.
CHARTDATA_GHG_SECTOR = "Chart 1"
CHARTDATA_GHG_SOURCE = "Chart 2"

#: Default scalar cost assumptions used when no cost table is on disk.
_INDICATOR_COST_DEFAULTS = {
    ("nuclear", "efficiency"): 0.33,
    ("gas", "CO2 intensity"): 0.198,
    ("methanolisation", "carbondioxide-input"): 1.37,
}

#: Real models, present on the developer's machine but not in the repository.
MODEL_ROOTS = {
    "negawatt": Path("/home/sylvain/svn/pypsa-eur_negawatt"),
    "pypsa-wal": Path("/home/sylvain/svn/pypsa-wal"),
}

NEGAWATT_RESOURCES = MODEL_ROOTS["negawatt"] / "resources" / "ref"


@dataclass
class IndicatorTestContext:
    """Minimal stand-in for :class:`~pypsa2html.context.BuildContext`."""

    config: object
    taxonomy: object = field(default_factory=load_taxonomy)
    nodes: object = field(
        default_factory=lambda: build_node_set(
            [],
            include=["BE", "DE", "FR", "GB", "NL"],
            labels={
                "BE": "Belgium",
                "DE": "Germany",
                "FR": "France",
                "GB": "Great Britain",
                "NL": "Netherlands",
            },
            focus="BE",
            aggregate_code="EU",
            aggregate_label="5 countries",
        )
    )
    resources_dir: Path = field(default_factory=lambda: NEGAWATT_RESOURCES)
    _files: dict = field(default_factory=dict)
    horizons: list[int] = field(default_factory=lambda: [2020, 2030, 2040, 2050])
    year_columns: list[str] = field(default_factory=lambda: ["2020", "2030", "2040", "2050"])

    def horizon_weights(self) -> pd.Series:
        gaps = [self.horizons[i + 1] - self.horizons[i] for i in range(len(self.horizons) - 1)]
        return pd.Series(gaps + [gaps[-1]], index=self.horizons, dtype=float)

    def is_aggregate(self, node: str) -> bool:
        return self.nodes.is_aggregate(node)

    def read_csv(self, relpath: str | Path, *, base: str = "results", **kwargs):
        path = self.resources_dir / relpath
        if not path.exists():
            return None
        return pd.read_csv(path, **kwargs)

    def cost(self, technology: str, parameter: str, default=None) -> float:
        value = _INDICATOR_COST_DEFAULTS.get((technology, parameter))
        if value is not None:
            return value
        if default is None:
            raise KeyError(f"no stub cost for ({technology!r}, {parameter!r})")
        return default


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


@pytest.fixture(scope="session")
def energy_flows_eu() -> pd.DataFrame:
    """Real négaWatt energy flow table for the aggregate node."""
    return pd.read_csv(DATA / "flows" / "EU_energy.csv")


@pytest.fixture(scope="session")
def carbon_flows_eu() -> pd.DataFrame:
    """Real négaWatt carbon flow table for the aggregate node."""
    return pd.read_csv(DATA / "flows" / "EU_carbon.csv")


@pytest.fixture(scope="session")
def chartdata_available() -> bool:
    return (CHARTDATA_ROOT / "ChartData_BE.xlsx").exists()


@pytest.fixture(scope="session")
def indicator_ctx() -> IndicatorTestContext:
    """Context stub with négaWatt horizons and taxonomy, no networks."""
    config = type(
        "Config",
        (),
        {
            "project": type("Project", (), {"decimals": 3})(),
            "model": type("Model", (), {"base_year": 2020, "clusters": "adm"})(),
            "output": type("Output", (), {"chart_data": False})(),
            "raw": {},
        },
    )()
    return IndicatorTestContext(config=config)


@pytest.fixture
def flow_tables(data_dir):
    def _load(node: str):
        return (
            pd.read_csv(data_dir / "flows" / f"{node}_energy.csv"),
            pd.read_csv(data_dir / "flows" / f"{node}_carbon.csv"),
        )

    return _load


@pytest.fixture
def built_indicators(indicator_ctx, flow_tables):
    from pypsa2html import indicators

    def _build(node: str = "BE"):
        energy, carbon = flow_tables(node)
        result = indicators.build(indicator_ctx, node, energy=energy, carbon=carbon)
        indicator_ctx._files[("indicators", node)] = result
        return result

    return _build


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
