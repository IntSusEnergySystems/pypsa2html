"""Extraction: solved networks -> tidy per-node flow tables.

Every extractor takes ``(ctx, node)`` and returns the long flow table
described in ``docs/INTERNALS.md`` §3 -- ``code, label, unit`` plus one column
per entry in ``ctx.year_columns``.  Extractors never write files, never touch
``snakemake`` and never mutate the networks they read.
"""

from .balance import derive_dispatch_window, dispatch_window, energy_balance
from .emissions import carbon_flows
from .flows import energy_flows
from .tables import capacity_table, cost_table, demand_table, utilisation_table

__all__ = [
    "energy_flows",
    "carbon_flows",
    "cost_table",
    "capacity_table",
    "demand_table",
    "utilisation_table",
    "energy_balance",
    "dispatch_window",
    "derive_dispatch_window",
]
