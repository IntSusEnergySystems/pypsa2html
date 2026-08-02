"""Node detection and resolution.

The substring matching this replaces was the single most fragile idiom in the
legacy code, so the collision cases get explicit coverage.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pypsa2html.nodes import (
    PSEUDO_LOCATIONS,
    NodeResolver,
    build_node_set,
    detect_locations,
)


class FakeNetwork:
    """Minimal stand-in exposing the attributes NodeResolver touches."""

    def __init__(self, buses: pd.DataFrame, links: pd.DataFrame | None = None):
        self.buses = buses
        self.links = links if links is not None else pd.DataFrame()

    def static(self, component: str) -> pd.DataFrame:
        return getattr(self, component)


@pytest.fixture
def belgian_network():
    """A sub-national model whose node codes share a prefix."""
    buses = pd.DataFrame(
        {
            "location": ["BEWAL", "BEVLG", "BEBRU", "DE", "EU", ""],
            "carrier": ["AC", "AC", "AC", "AC", "oil", "none"],
        },
        index=["BEWAL", "BEVLG", "BEBRU", "DE", "EU oil", "orphan"],
    )
    links = pd.DataFrame(
        {"bus0": ["BEWAL", "BEVLG", "BEWAL", "DE"], "bus1": ["DE", "BEWAL", "BEVLG", "BEWAL"]},
        index=["BEWAL CCGT", "BEVLG CCGT", "BEWAL-BEVLG line", "DE CCGT"],
    )
    return FakeNetwork(buses, links)


def test_detect_excludes_pseudo_locations(belgian_network):
    assert detect_locations(belgian_network) == ["BEBRU", "BEVLG", "BEWAL", "DE"]
    assert "EU" in PSEUDO_LOCATIONS and "" in PSEUDO_LOCATIONS


def test_detect_requires_location_column():
    network = FakeNetwork(pd.DataFrame({"carrier": ["AC"]}, index=["x"]))
    with pytest.raises(ValueError, match="no 'location' column"):
        detect_locations(network)


def test_location_strategy_does_not_confuse_prefixes(belgian_network):
    """`filter(like="BEWAL")` on names would also catch 'BEWAL-BEVLG line'."""
    resolver = NodeResolver(belgian_network, "location")
    assert set(resolver.select("links", "BEWAL")) == {"BEWAL CCGT", "BEWAL-BEVLG line"}
    assert set(resolver.select("links", "BEVLG")) == {"BEVLG CCGT"}
    assert set(resolver.select("links", "DE")) == {"DE CCGT"}


def test_substring_strategy_reproduces_the_legacy_collision(belgian_network):
    """Documents why 'location' is the default."""
    legacy = NodeResolver(belgian_network, "substring")
    # 'BEVLG CCGT' is not matched, but the shared line is attributed to both.
    assert "BEWAL-BEVLG line" in set(legacy.select("links", "BEWAL"))
    assert "BEWAL-BEVLG line" in set(legacy.select("links", "BEVLG"))


def test_substring_strategy_over_matches_a_short_code():
    buses = pd.DataFrame({"location": ["BE", "BEWAL"]}, index=["BE", "BEWAL"])
    links = pd.DataFrame({"bus0": ["BE", "BEWAL"]}, index=["BE CCGT", "BEWAL CCGT"])
    legacy = NodeResolver(FakeNetwork(buses, links), "substring")
    assert set(legacy.select("links", "BE")) == {"BE CCGT", "BEWAL CCGT"}  # the bug

    modern = NodeResolver(FakeNetwork(buses, links), "location")
    assert set(modern.select("links", "BE")) == {"BE CCGT"}  # fixed


def test_unknown_strategy_rejected(belgian_network):
    with pytest.raises(ValueError, match="unknown node resolution strategy"):
        NodeResolver(belgian_network, "regex")


# -- NodeSet ---------------------------------------------------------------

def test_build_appends_aggregate_last():
    ns = build_node_set(["BE", "DE"], aggregate_code="ALL")
    assert ns.codes == ["BE", "DE", "ALL"]
    assert ns.real_codes == ["BE", "DE"]
    assert ns.is_aggregate("ALL") and not ns.is_aggregate("BE")


def test_aggregate_can_be_disabled():
    ns = build_node_set(["BE", "DE"], aggregate_code=None)
    assert ns.codes == ["BE", "DE"]


def test_aggregate_colliding_with_a_real_node_is_rejected():
    """'EU' is a real PyPSA-Eur location as well as the legacy aggregate name."""
    with pytest.raises(ValueError, match="collides with a real model node"):
        build_node_set(["BE", "EU"], aggregate_code="EU")


def test_include_overrides_detection():
    ns = build_node_set(["BE", "DE", "FR"], include=["FR", "BE"], aggregate_code=None)
    assert ns.codes == ["FR", "BE"]


def test_exclude_subtracts():
    ns = build_node_set(["BE", "DE", "FR"], exclude=["DE"], aggregate_code=None)
    assert ns.codes == ["BE", "FR"]


def test_focus_defaults_to_first_node():
    assert build_node_set(["DE", "BE"], aggregate_code=None).focus == "DE"


def test_focus_must_exist():
    with pytest.raises(ValueError, match="nodes.focus"):
        build_node_set(["BE", "DE"], focus="FR", aggregate_code=None)


def test_empty_selection_is_an_error():
    with pytest.raises(ValueError, match="no nodes left"):
        build_node_set(["BE"], exclude=["BE"])


def test_labels_applied_and_default_to_code():
    ns = build_node_set(["BE", "DE"], labels={"BE": "Belgium"}, aggregate_code=None)
    assert ns["BE"].label == "Belgium"
    assert ns["DE"].label == "DE"


def test_lookup_of_unknown_node_lists_the_known_ones():
    ns = build_node_set(["BE"], aggregate_code=None)
    with pytest.raises(KeyError, match="unknown node"):
        ns["ZZ"]


def test_detection_does_not_mutate_its_input():
    """ALL_COUNTRIES.append('EU') mutated snakemake.params.countries in place."""
    detected = ["BE", "DE"]
    build_node_set(detected, aggregate_code="ALL")
    assert detected == ["BE", "DE"]
