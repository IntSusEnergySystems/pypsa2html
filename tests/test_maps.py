"""Map chart builders resolve; optional end-to-end test with a real model."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pypsa2html.build import resolve_builder
from pypsa2html.charts.base import Html
from pypsa2html.charts.maps import (
    _hash_parts,
    _reuse_map_png,
    clear_map_png_cache,
)
from pypsa2html.charts.maps_pipes import _vendored_group_pipes, group_pipes
from tests.conftest import requires_model

MAP_BUILDERS = ("maps.costs", "maps.hydrogen", "maps.gas")


@pytest.mark.parametrize("builder_name", MAP_BUILDERS)
def test_map_builder_resolves(builder_name):
    assert resolve_builder(builder_name) is not None


def test_map_builders_return_none_without_networks(indicator_ctx):
    """Fixture context has no networks; maps degrade gracefully."""
    from pypsa2html.pages import load_manifest

    builders = {s.builder: s for p in load_manifest() for s in p.sections if s.builder in MAP_BUILDERS}
    for name in MAP_BUILDERS:
        section = builders[name]
        builder = resolve_builder(name)
        assert builder(indicator_ctx, "BE", section) is None


def test_group_pipes_sums_parallel_capacities():
    df = pd.DataFrame(
        {
            "bus0": ["A H2", "A H2"],
            "bus1": ["B H2", "B H2"],
            "p_nom_opt": [100.0, 50.0],
        },
        index=["p1", "p2"],
    )
    grouped = _vendored_group_pipes(df)
    assert len(grouped) == 1
    assert grouped["p_nom_opt"].iloc[0] == pytest.approx(150.0)
    assert callable(group_pipes)


def test_map_png_reuse_hardlinks_or_copies(tmp_path):
    clear_map_png_cache()
    src = tmp_path / "a.png"
    src.write_bytes(b"png-bytes")
    from pypsa2html.charts import maps as maps_mod

    maps_mod._MAP_PNG_BY_FINGERPRINT["fp1"] = src
    dest = tmp_path / "b.png"
    assert _reuse_map_png("fp1", dest)
    assert dest.read_bytes() == b"png-bytes"
    assert not _reuse_map_png("missing", tmp_path / "c.png")
    clear_map_png_cache()


def test_hash_parts_stable_for_series():
    s = pd.Series([1.0, 2.0], index=["a", "b"])
    assert _hash_parts("x", s) == _hash_parts("x", s.copy())
    assert _hash_parts("x", s) != _hash_parts("x", s * 2)


@pytest.mark.needs_model
@requires_model("negawatt")
def test_map_costs_renders_with_negawatt_model(tmp_path):
    from pypsa2html.config import load_config
    from pypsa2html.context import build_context
    from pypsa2html.pages import load_manifest

    config_path = Path(__file__).resolve().parents[1] / "config" / "negawatt.yaml"
    config = load_config(config_path)
    config.output.dir = str(tmp_path / "html")
    ctx = build_context(config, "ref")

    section = next(
        s for p in load_manifest() for s in p.sections if s.builder == "maps.costs"
    )
    builder = resolve_builder("maps.costs")
    result = builder(ctx, "BE", section)
    if result is None:
        pytest.skip("maps extras or geojson unavailable")
    assert isinstance(result, Html)
    assert "map_map_costs_" in result.fragment
    assert any(tmp_path.glob("html/map_map_costs_*.png"))
