"""Config loading, the page manifest, and horizon arithmetic."""

from __future__ import annotations

import pandas as pd
import pytest
import yaml

from pypsa2html.config import deep_merge, load_config
from pypsa2html.context import _normalise_costs
from pypsa2html.networks import discover_horizons
from pypsa2html.pages import load_manifest

# -- config ----------------------------------------------------------------

def test_deep_merge_is_recursive_and_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = deep_merge(base, {"a": {"y": 9}})
    assert out == {"a": {"x": 1, "y": 9}, "b": 3}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_defaults_are_applied(minimal_config):
    cfg = load_config(minimal_config)
    assert cfg.project.decimals == 3          # from default.yaml
    assert cfg.project.name == "Demo"         # overridden
    assert cfg.output.plotly == "cdn"
    assert cfg.nodes.resolution == "location"


def test_landing_defaults_to_first_scenario(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [
                    {"name": "one", "results_dir": "r1"},
                    {"name": "two", "results_dir": "r2"},
                ]
            }
        )
    )
    assert load_config(path).landing_scenario.name == "one"


def test_unknown_landing_scenario_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [{"name": "one", "results_dir": "r"}],
                "landing": {"scenario": "typo"},
            }
        )
    )
    with pytest.raises(ValueError, match="landing.scenario"):
        load_config(path)


def test_unknown_config_key_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [{"name": "one", "results_dir": "r"}],
                "output": {"plotlyy": "cdn"},
            }
        )
    )
    with pytest.raises(ValueError, match="unknown key"):
        load_config(path)


def test_scenario_without_results_dir_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"scenarios": [{"name": "one"}]}))
    with pytest.raises(ValueError, match="no results_dir"):
        load_config(path)


def test_no_scenarios_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"project": {"name": "x"}}))
    with pytest.raises(ValueError, match="at least one entry"):
        load_config(path)


def test_network_path_uses_the_pattern(minimal_config):
    cfg = load_config(minimal_config)
    path = cfg.model.network_path("/tmp/res", 2040)
    assert path.name == "base_s_adm___2040.nc"


# -- horizon discovery -----------------------------------------------------

def test_discover_horizons(tmp_path):
    net = tmp_path / "networks"
    net.mkdir()
    for name in ("base_s_adm___2030.nc", "base_s_adm___2050.nc", "unrelated.nc"):
        (net / name).write_bytes(b"")
    found = discover_horizons(
        tmp_path,
        "networks/base_s_{clusters}_{opts}_{sector_opts}_{horizon}.nc",
        clusters="adm",
        opts="",
        sector_opts="",
    )
    assert found == [2030, 2050]


def test_discover_horizons_ignores_a_different_clustering(tmp_path):
    net = tmp_path / "networks"
    net.mkdir()
    (net / "base_s_adm___2030.nc").write_bytes(b"")
    (net / "base_s_6___2030.nc").write_bytes(b"")
    found = discover_horizons(
        tmp_path,
        "networks/base_s_{clusters}_{opts}_{sector_opts}_{horizon}.nc",
        clusters="adm",
        opts="",
        sector_opts="",
    )
    assert found == [2030]


def test_network_cache_defaults_to_all_horizons(tmp_path):
    """Default maxsize must hold every horizon — an LRU of 2 thrashing is costly."""
    from pypsa2html.networks import NetworkCache, clear_path_cache

    clear_path_cache()
    paths = {h: tmp_path / f"{h}.nc" for h in (2025, 2030, 2040, 2050)}
    for p in paths.values():
        p.write_bytes(b"")
    cache = NetworkCache(paths)  # maxsize=None → len(paths)
    assert cache.maxsize == 4
    bounded = NetworkCache(paths, maxsize=2)
    assert bounded.maxsize == 2


# -- cost table normalisation ---------------------------------------------

def test_normalise_costs_from_wide_layout():
    """float(options.loc[(tech, param)]) raised from pandas 2.2 on."""
    wide = pd.DataFrame(
        {"technology": ["gas", "oil"], "CO2 intensity": [0.198, 0.26], "FOM": [1.0, 2.0]}
    )
    out = _normalise_costs(wide)
    assert float(out.at[("gas", "CO2 intensity"), "value"]) == pytest.approx(0.198)


def test_normalise_costs_from_long_layout():
    long = pd.DataFrame(
        {
            "technology": ["gas", "gas"],
            "parameter": ["CO2 intensity", "FOM"],
            "value": [0.198, 1.0],
        }
    )
    out = _normalise_costs(long)
    assert float(out.at[("gas", "CO2 intensity"), "value"]) == pytest.approx(0.198)


# -- page manifest ---------------------------------------------------------

def test_manifest_section_ids_are_unique():
    ids = load_manifest().section_ids()
    assert len(ids) == len(set(ids))


def test_disabled_section_is_absent_not_falsy():
    full = load_manifest()
    trimmed = load_manifest(enable={"sankey_carbon": False})
    assert "sankey_carbon" in full.section_ids()
    assert "sankey_carbon" not in trimmed.section_ids()


def test_unknown_plot_key_is_rejected():
    # 'Cummulative Emissions' was misspelled in plots.yaml and the misspelling
    # became load-bearing because a wrong key silently did nothing.
    with pytest.raises(ValueError, match="unknown section id"):
        load_manifest(enable={"sankey_carbonn": False})


def test_page_selection_and_ordering():
    m = load_manifest(include_pages=["costs", "emissions"])
    assert m.ids == ["costs", "emissions"]


def test_unknown_page_is_rejected():
    with pytest.raises(ValueError, match="unknown page"):
        load_manifest(include_pages=["cost"])


def test_page_with_all_sections_disabled_is_dropped():
    m = load_manifest(enable={"sankey_energy": False, "sankey_carbon": False})
    assert "sankeys" not in m.ids


def test_filenames_follow_the_convention():
    m = load_manifest()
    assert m["emissions"].filename("BEWAL", "ref") == "BEWAL_emissions_ref.html"
    # Shared pages drop the node prefix -- see DESIGN_DECISIONS D8.
    assert m["maps"].shared
    assert m["maps"].filename("BEWAL", "ref") == "maps_ref.html"


def test_every_builder_is_well_formed():
    for page in load_manifest():
        for section in page.sections:
            module, _, func = section.builder.rpartition(".")
            assert module and func, section.builder


def test_section_scope():
    m = load_manifest()
    section = next(s for s in m["overview"].sections if s.id == "energy_independence")

    class Node:
        def __init__(self, aggregate):
            self.aggregate = aggregate

    assert section.scope == "all"
    assert section.applies_to(Node(False))
    assert section.applies_to(Node(True))

    ss = next(s for s in m["fec"].sections if s.id == "self_sufficiency")
    assert ss.scope == "all"
    assert ss.applies_to(Node(True))


def test_groups_are_loaded_from_yaml(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [{"name": "one", "results_dir": "r"}],
                "nodes": {
                    "detect": False,
                    "include": ["BEVLG", "BEWAL", "DE"],
                    "groups": [
                        {
                            "code": "BE",
                            "label": "Belgium",
                            "members": ["BEVLG", "BEWAL", "BEBRU"],
                        }
                    ],
                },
            }
        )
    )
    cfg = load_config(path)
    assert len(cfg.nodes.groups) == 1
    assert cfg.nodes.groups[0].code == "BE"
    assert cfg.nodes.groups[0].members == ["BEVLG", "BEWAL", "BEBRU"]
    assert cfg.features.price_abs_cap == 1.0e4
    assert cfg.features.nuclear_primary == "uranium"


def test_group_prefix_key_rejected_in_config(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenarios": [{"name": "one", "results_dir": "r"}],
                "nodes": {
                    "groups": [{"code": "BE", "members": ["BEVLG"], "prefix": "BE"}],
                },
            }
        )
    )
    with pytest.raises(ValueError, match="unknown key"):
        load_config(path)


def test_negawatt_example_declares_be_group_and_eu_study_wide():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "negawatt.yaml")
    assert cfg.nodes.focus == "BE"
    assert cfg.landing.node == "BE"
    assert cfg.nodes.aggregate.code == "EU"
    assert [g.code for g in cfg.nodes.groups] == ["BE"]
    assert cfg.nodes.groups[0].members == ["BEVLG", "BEWAL", "BEBRU"]
    assert "BEVLG" in cfg.nodes.labels


def test_pypsa_wal_example_declares_be_group():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "pypsa-wal.yaml")
    assert cfg.nodes.focus == "BEWAL"
    assert cfg.nodes.aggregate.code == "ALL"
    assert [g.code for g in cfg.nodes.groups] == ["BE"]
    assert cfg.nodes.groups[0].members == ["BEVLG", "BEWAL", "BEBRU"]
