"""Extraction: solved networks -> tidy per-node flow tables.

Every extractor takes ``(ctx, node)`` and returns the long flow table
described in ``docs/INTERNALS.md`` §3 -- ``code, label, unit`` plus one column
per entry in ``ctx.year_columns``.  Extractors never write files, never touch
``snakemake`` and never mutate the networks they read.
"""

from .emissions import carbon_flows
from .flows import energy_flows

__all__ = ["energy_flows", "carbon_flows"]
