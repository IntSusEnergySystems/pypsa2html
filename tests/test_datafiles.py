"""The packaged taxonomy must load, validate, and stay internally consistent.

Several of these encode bugs that were live in the Excel original, so they
double as regression tests against reintroducing them.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pypsa2html.datafiles import NODE_TYPES, TaxonomyError, load_taxonomy


@pytest.fixture(scope="module")
def tax():
    return load_taxonomy()


def test_loads_and_validates(tax):
    assert len(tax.nodes) > 100
    assert not tax.nodes.index.duplicated().any()


def test_every_node_type_is_known(tax):
    assert set(tax.nodes["Type"].dropna()) <= NODE_TYPES


@pytest.mark.parametrize(
    "node_type,expected_member",
    [
        ("FINAL_ENERGIES", "elc_fe"),
        ("PRIMARY_ENERGIES", "gaz_pe"),
        ("SECONDARY_ENERGIES", "elc_se"),
        ("DEMAND_SECTORS", "ind"),
        ("GHG_SECTORS", "atm"),
    ],
)
def test_codes_of_type(tax, node_type, expected_member):
    codes = tax.codes_of_type(node_type)
    assert expected_member in codes


def test_codes_of_type_rejects_unknown(tax):
    # The legacy nodes_by_type() returned [] for a typo, so a whole node class
    # silently vanished from the Sankey.
    with pytest.raises(TaxonomyError, match="unknown node type"):
        tax.codes_of_type("FINAL_ENERGY")  # missing trailing S


@pytest.mark.parametrize(
    "table", ["processes_energy", "processes_carbon", "processes_ghg"]
)
def test_process_edges_reference_known_nodes(tax, table):
    edges = getattr(tax, table)
    known = set(tax.nodes.index)
    assert set(edges["Source"].dropna()) <= known
    assert set(edges["Target"].dropna()) <= known


def test_process_type_is_empty_string_not_nan(tax):
    # A NaN Type propagated into MultiIndex.from_tuples and produced keys that
    # never matched the ('src','tgt','') literals used downstream.
    for table in (tax.processes_energy, tax.processes_carbon, tax.processes_ghg):
        assert not table["Type"].isna().any()


def test_colors_are_hex(tax):
    colors = tax.colors.dropna().astype(str)
    bad = colors[~colors.str.match(r"^#[0-9A-Fa-f]{6}$")]
    assert bad.empty, f"non-hex colours: {bad.to_dict()}"


def test_sankey_positions_in_range(tax):
    for axis in ("PositionX", "PositionY"):
        pos = pd.to_numeric(tax.nodes[axis], errors="coerce").dropna()
        assert ((pos >= 0) & (pos <= 1)).all()


@pytest.mark.parametrize("table", ["carrier_flows_energy", "carrier_flows_carbon"])
def test_carrier_flow_entries_unique(tax, table):
    # 'urban decentral biomass boiler_2' appeared twice in the original
    # entries_to_select, so its value was summed into lossbbb twice.
    entries = getattr(tax, table)["entry"]
    assert not entries.duplicated().any()


@pytest.mark.parametrize("table", ["carrier_flows_energy", "carrier_flows_carbon"])
def test_carrier_flow_codes_non_empty(tax, table):
    codes = getattr(tax, table)["code"]
    assert codes.notna().all() and (codes.astype(str).str.len() > 0).all()


def test_regions_is_the_country_table_not_the_description_sheet(tax):
    # pd.read_excel(COUNTRIES.xlsx, index_col=0) silently read the first sheet,
    # which is a documentation table, so ISO_Code/Label were never available.
    assert {"Label", "ISO_Code", "Color"} <= set(tax.regions.columns)
    assert tax.regions.at["BE", "ISO_Code"] == "BEL"


def test_rename_map_maps_model_code_to_internal_code(tax):
    mapping = tax.rename_map()
    # Most rows are identity; the sheet exists for the nine that are not.
    assert mapping["pop"] == "pop"
    assert mapping["perrescha"] == "perresvap"
    assert mapping["prespcenrcfcltra"] == "pcenrcfcltra"
    assert mapping["solghgco2luf"] == "ghgco2luf"


def test_rename_map_is_injective(tax):
    # Two model codes collapsing onto one internal code would silently make
    # one indicator overwrite the other.
    mapping = tax.rename_map()
    assert len(set(mapping.values())) == len(mapping)


def test_load_is_cached():
    assert load_taxonomy() is load_taxonomy()
