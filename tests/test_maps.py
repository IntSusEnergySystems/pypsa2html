"""Map chart builders resolve; optional end-to-end test with a real model."""

from __future__ import annotations

from pathlib import Path

import pytest

from pypsa2html.build import resolve_builder
from pypsa2html.charts.base import Html
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
