"""Loaders for the packaged taxonomy data.

The legacy tool kept its energy-system vocabulary in a binary
``the legacy config workbook``.  That made review, diffing and merging impossible, and a
typo in a node code produced a silently missing Sankey link rather than an
error.  The same tables now ship as CSVs under ``pypsa2html/data/`` and are
validated on load.

Tables
------
``nodes.csv``
    The vocabulary: one row per box in the Sankey / series in a chart.
``processes_energy.csv`` / ``processes_carbon.csv`` / ``processes_ghg.csv``
    Edge lists over those nodes (energy flows, carbon flows, GHG attribution).
``indicators.csv``
    Scalar indicators and the rename map from model codes to internal codes.
``carrier_flows_energy.csv`` / ``carrier_flows_carbon.csv``
    PyPSA carrier/port -> internal code, used by the extraction step.
``regions.csv``
    Labels, colours and ISO codes for known regions (used by the choropleths).
``domestic_gas_production.csv`` / ``domestic_oil_production.csv``
    Exogenous domestic fossil production per region and horizon, in TWh/year.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

#: The ``Type`` values recognised in ``nodes.csv``.
NODE_TYPES = frozenset(
    {
        "GROUPS",
        "DEMAND_SECTORS",
        "SECONDARY_ENERGIES",
        "FINAL_ENERGIES",
        "PRIMARY_ENERGIES",
        "LOCAL_PROD",
        "IMPORTS",
        "EXPORTS",
        "SECONDARY_IMPORTS",
        "OTHER",
        "GHG",
        "GHG_SECTORS",
    }
)


class TaxonomyError(ValueError):
    """Raised when a packaged or user-supplied taxonomy table is inconsistent."""


def _read(name: str, data_dir: Path | None = None) -> pd.DataFrame:
    path = (data_dir or DATA_DIR) / name
    if not path.exists():
        raise FileNotFoundError(f"taxonomy table not found: {path}")
    return pd.read_csv(path)


@dataclass
class Taxonomy:
    """The validated energy-system vocabulary and its edge lists."""

    nodes: pd.DataFrame
    processes_energy: pd.DataFrame
    processes_carbon: pd.DataFrame
    processes_ghg: pd.DataFrame
    indicators: pd.DataFrame
    carrier_flows_energy: pd.DataFrame
    carrier_flows_carbon: pd.DataFrame
    regions: pd.DataFrame
    domestic_gas: pd.DataFrame
    domestic_oil: pd.DataFrame

    # -- lookups ----------------------------------------------------------
    def codes_of_type(self, node_type: str) -> list[str]:
        """Node codes whose ``Type`` equals ``node_type``.

        Replaces the legacy ``nodes_by_type``.  Unlike the original this
        raises on an unknown type instead of quietly returning ``[]``.
        """
        if node_type not in NODE_TYPES:
            raise TaxonomyError(
                f"unknown node type {node_type!r}; known types: {sorted(NODE_TYPES)}"
            )
        return self.nodes.index[self.nodes["Type"] == node_type].tolist()

    @property
    def labels(self) -> pd.Series:
        return self.nodes["Label"]

    @property
    def colors(self) -> pd.Series:
        return self.nodes["Color"]

    def label(self, code: str) -> str:
        try:
            return str(self.nodes.at[code, "Label"])
        except KeyError:
            return code

    def rename_map(self) -> dict[str, str]:
        """Model ``Value_Code`` -> internal ``Code``, from ``indicators.csv``."""
        ind = self.indicators
        return dict(zip(ind["Value_Code"], ind.index, strict=True))


def _validate(tax: Taxonomy) -> None:
    nodes = tax.nodes

    dupes = nodes.index[nodes.index.duplicated()].tolist()
    if dupes:
        raise TaxonomyError(f"duplicate node codes in nodes.csv: {sorted(set(dupes))}")

    bad_types = sorted(set(nodes["Type"].dropna()) - NODE_TYPES)
    if bad_types:
        raise TaxonomyError(
            f"unknown Type value(s) in nodes.csv: {bad_types}. Known: {sorted(NODE_TYPES)}"
        )

    known = set(nodes.index)
    for name, edges in (
        ("processes_energy", tax.processes_energy),
        ("processes_carbon", tax.processes_carbon),
        ("processes_ghg", tax.processes_ghg),
    ):
        for col in ("Source", "Target"):
            missing = sorted(set(edges[col].dropna()) - known)
            if missing:
                raise TaxonomyError(
                    f"{name}.csv references node code(s) absent from nodes.csv "
                    f"in column {col}: {missing}"
                )

    bad_colors = nodes["Color"].dropna()
    bad_colors = bad_colors[~bad_colors.astype(str).str.match(r"^#[0-9A-Fa-f]{6}$")]
    if len(bad_colors):
        logger.warning(
            "nodes.csv: %d colour value(s) are not #rrggbb and will fall back to "
            "the plotly default: %s",
            len(bad_colors),
            bad_colors.head(5).to_dict(),
        )

    for axis in ("PositionX", "PositionY"):
        pos = pd.to_numeric(nodes[axis], errors="coerce").dropna()
        out_of_range = pos[(pos < 0) | (pos > 1)]
        if len(out_of_range):
            raise TaxonomyError(
                f"nodes.csv: {axis} must be within [0, 1]; offending rows: "
                f"{out_of_range.to_dict()}"
            )

    for name, table in (
        ("carrier_flows_energy", tax.carrier_flows_energy),
        ("carrier_flows_carbon", tax.carrier_flows_carbon),
    ):
        dup = table["entry"][table["entry"].duplicated()].tolist()
        if dup:
            raise TaxonomyError(f"{name}.csv: duplicate entry values {dup}")
        blank = table.index[table["code"].isna() | (table["code"].astype(str) == "")]
        if len(blank):
            raise TaxonomyError(
                f"{name}.csv: {len(blank)} row(s) have an empty 'code'. In legacy "
                "the legacy tool these silently collapsed into one blank row; they are now "
                f"an error. Offending entries: {table.loc[blank, 'entry'].tolist()}"
            )


@functools.lru_cache(maxsize=4)
def load_taxonomy(data_dir: str | None = None) -> Taxonomy:
    """Load and validate the taxonomy tables (cached).

    ``data_dir`` overrides the packaged directory, so a project can ship its
    own vocabulary without forking the library.
    """
    d = Path(data_dir) if data_dir else None

    nodes = _read("nodes.csv", d).set_index("Code")
    indicators = _read("indicators.csv", d).set_index("Code")

    def edges(name: str) -> pd.DataFrame:
        df = _read(name, d)
        # A blank Type is the discriminator for "the only edge between this
        # Source/Target pair".  Keep it as '' rather than NaN so tuple keys
        # built from it match the ('src', 'tgt', '') literals used downstream.
        df["Type"] = df["Type"].fillna("")
        return df

    tax = Taxonomy(
        nodes=nodes,
        processes_energy=edges("processes_energy.csv"),
        processes_carbon=edges("processes_carbon.csv"),
        processes_ghg=edges("processes_ghg.csv"),
        indicators=indicators,
        carrier_flows_energy=_read("carrier_flows_energy.csv", d),
        carrier_flows_carbon=_read("carrier_flows_carbon.csv", d),
        regions=_read("regions.csv", d).set_index("Code"),
        domestic_gas=_read("domestic_gas_production.csv", d).set_index("region"),
        domestic_oil=_read("domestic_oil_production.csv", d).set_index("region"),
    )
    _validate(tax)
    return tax
