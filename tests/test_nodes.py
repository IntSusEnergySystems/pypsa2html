"""Node detection and resolution.

The substring matching this replaces was the single most fragile idiom in the
legacy code, so the collision cases get explicit coverage.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.context import BuildContext
from pypsa2html.extract.flows import _select
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


# -- Group aggregates ------------------------------------------------------

_NUTS1 = ["BEVLG", "BEWAL", "BEBRU"]
_DETECTED = ["BEBRU", "BEVLG", "BEWAL", "DE"]
_BE_GROUP = {"code": "BE", "label": "Belgium", "members": ["BEVLG", "BEWAL", "BEBRU"]}


def _nuts1_set(**kwargs):
    return build_node_set(
        _DETECTED,
        labels={
            "BEVLG": "Flanders",
            "BEWAL": "Wallonia",
            "BEBRU": "Brussels",
            "BE": "Belgium",
            "DE": "Germany",
        },
        groups=[_BE_GROUP],
        aggregate_code="EU",
        aggregate_label="5 countries",
        **kwargs,
    )


def test_group_members_are_sorted_and_is_aggregate():
    ns = _nuts1_set(focus="BE")
    assert ns.members_of("BE") == ["BEBRU", "BEVLG", "BEWAL"]
    assert ns["BE"].members == ("BEBRU", "BEVLG", "BEWAL")
    assert ns.is_aggregate("BE")
    assert ns.is_aggregate("EU")
    assert not ns.is_aggregate("BEWAL")
    assert not ns.is_aggregate("DE")
    assert ns.is_study_wide("EU")
    assert not ns.is_study_wide("BE")
    assert ns.locations_for("BE") == ["BEBRU", "BEVLG", "BEWAL"]
    assert ns.locations_for("EU") is None
    assert ns.locations_for("DE") == ["DE"]
    assert ns.members_of("EU") is None
    assert ns.members_of("DE") is None
    assert ns.focus == "BE"
    assert ns.real_codes == ["BEBRU", "BEVLG", "BEWAL", "DE"]
    # Groups sit after real nodes; study-wide aggregate is last.
    assert ns.codes == ["BEBRU", "BEVLG", "BEWAL", "DE", "BE", "EU"]


def test_group_code_colliding_with_a_real_node_is_omitted(caplog):
    ns = build_node_set(
        ["BE", "DE"],
        groups=[{"code": "BE", "members": ["BE"]}],
        aggregate_code=None,
    )
    assert ns.codes == ["BE", "DE"]
    assert not ns.is_aggregate("BE")
    assert "omitted" in caplog.text


def test_group_code_colliding_with_study_wide_is_rejected():
    with pytest.raises(ValueError, match="collides"):
        build_node_set(
            ["BEVLG", "DE"],
            groups=[{"code": "ALL", "members": ["BEVLG"]}],
            aggregate_code="ALL",
        )


def test_prefix_key_on_a_group_is_rejected():
    with pytest.raises(ValueError, match="prefix matching"):
        build_node_set(
            _DETECTED,
            groups=[{"code": "BE", "members": _NUTS1, "prefix": "BE"}],
        )


def test_missing_group_member_is_skipped(caplog):
    ns = build_node_set(
        ["BEVLG", "DE"],
        groups=[{"code": "BE", "label": "Belgium", "members": ["BEVLG", "BEWAL", "BEBRU"]}],
        aggregate_code=None,
        focus="BE",
    )
    assert ns.members_of("BE") == ["BEVLG"]
    assert "BEWAL" in caplog.text or "BEBRU" in caplog.text


def test_group_with_no_remaining_members_is_omitted(caplog):
    ns = build_node_set(
        ["DE"],
        groups=[{"code": "BE", "members": ["BEVLG", "BEWAL"]}],
        aggregate_code=None,
    )
    assert "BE" not in ns.codes
    assert "omitted" in caplog.text


def _ctx_for_network(network, ns):
    return BuildContext(
        config=SimpleNamespace(nodes=SimpleNamespace(resolution="location")),
        scenario=SimpleNamespace(name="demo"),
        taxonomy=None,
        nodes=ns,
        networks={2030: network},
        results_dir=".",
        resources_dir=".",
    )


def test_component_index_group_excludes_other_countries(belgian_network):
    ns = _nuts1_set(focus="BE")
    ctx = _ctx_for_network(belgian_network, ns)
    be = set(ctx.component_index(2030, "links", "BE"))
    assert be == {"BEWAL CCGT", "BEVLG CCGT", "BEWAL-BEVLG line"}
    assert "DE CCGT" not in be
    de = set(ctx.component_index(2030, "links", "DE"))
    assert de == {"DE CCGT"}
    everything = set(ctx.component_index(2030, "links", "EU"))
    assert "DE CCGT" in everything
    assert be <= everything


def test_select_helper_matches_component_index(belgian_network):
    ns = _nuts1_set(focus="BE")
    ctx = _ctx_for_network(belgian_network, ns)
    selected = set(_select(ctx, belgian_network, "BE", "links", 2030))
    assert selected == set(ctx.component_index(2030, "links", "BE"))
    assert "DE CCGT" not in selected


def test_first_real_link_nodes_does_not_use_eu_hub():
    buses = pd.DataFrame(
        {"location": ["EU", "BEWAL", "DE"]},
        index=["EU oil", "BEWAL naphtha for industry", "DE naphtha for industry"],
    )
    links = pd.DataFrame(
        {
            "bus0": ["EU oil", "EU oil"],
            "bus1": ["BEWAL naphtha for industry", "DE naphtha for industry"],
        },
        index=["BEWAL naphtha for industry", "DE naphtha for industry"],
    )
    network = FakeNetwork(buses, links)
    ns = _nuts1_set(focus="BE")
    ctx = _ctx_for_network(network, ns)
    be = set(ctx.component_index(2030, "links", "BE"))
    assert be == {"BEWAL naphtha for industry"}
    assert "DE naphtha for industry" not in be
