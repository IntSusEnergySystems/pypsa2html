"""Manifest chart builders resolve and run against fixture data."""

from __future__ import annotations

import plotly.graph_objects as go
import pytest

from pypsa2html import indicators
from pypsa2html.build import resolve_builder
from pypsa2html.charts.base import Html
from pypsa2html.pages import load_manifest

MANIFEST_SECTIONS = [
    (page.id, section)
    for page in load_manifest()
    for section in page.sections
]

RESOLVABLE_SECTIONS = [
    (page_id, section)
    for page_id, section in MANIFEST_SECTIONS
    if resolve_builder(section.builder) is not None
]


def _cache_indicators(ctx, node, energy, carbon):
    ctx._files[("indicators", node)] = indicators.build(
        ctx, node, energy=energy, carbon=carbon
    )


@pytest.fixture(scope="session")
def chart_ctx(indicator_ctx, energy_flows_be, carbon_flows_be):
    """Indicator context with BE flow tables cached for chart builders."""
    _cache_indicators(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    return indicator_ctx


@pytest.mark.parametrize(
    ("page_id", "section"),
    MANIFEST_SECTIONS,
    ids=[f"{page_id}.{section.id}" for page_id, section in MANIFEST_SECTIONS],
)
def test_manifest_builder_resolves_or_is_unimplemented(page_id, section):
    builder = resolve_builder(section.builder)
    if builder is None:
        pytest.skip("builder not yet implemented")
    assert callable(builder)


@pytest.mark.parametrize(
    ("page_id", "section"),
    RESOLVABLE_SECTIONS,
    ids=[f"{page_id}.{section.id}" for page_id, section in RESOLVABLE_SECTIONS],
)
def test_resolvable_builder_returns_chart_result(chart_ctx, page_id, section):
    builder = resolve_builder(section.builder)
    assert callable(builder)
    result = builder(chart_ctx, "BE", section)
    assert result is None or isinstance(result, (go.Figure, Html))
