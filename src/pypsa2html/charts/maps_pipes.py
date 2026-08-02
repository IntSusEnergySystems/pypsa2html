"""H2 pipe grouping: prefer PyPSA-Eur upstream, fall back to a vendored copy.

``group_pipes`` is byte-identical to PyPSA-Eur's
``scripts/plot_hydrogen_network.py``.  Importing it when that package is on
``sys.path`` avoids drifting from upstream; the vendored body is the fallback
when building outside a PyPSA-Eur checkout.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

import pandas as pd

logger = logging.getLogger(__name__)


def _vendored_group_pipes(df: pd.DataFrame, drop_direction: bool = False) -> pd.DataFrame:
    """Group pipes which connect the same buses and return overall capacity."""
    df = df.copy()
    if drop_direction:
        positive_order = df.bus0 < df.bus1
        df_p = df[positive_order]
        swap_buses = {"bus0": "bus1", "bus1": "bus0"}
        df_n = df[~positive_order].rename(columns=swap_buses)
        df = pd.concat([df_p, df_n])

    df["index_orig"] = df.index
    df.index = df.apply(
        lambda x: (
            f"H2 pipeline {str(x.bus0).replace(' H2', '')}"
            f" -> {str(x.bus1).replace(' H2', '')}"
        ),
        axis=1,
    )
    return df.groupby(level=0).agg(
        {"p_nom_opt": "sum", "bus0": "first", "bus1": "first", "index_orig": "first"}
    )


def _resolve_group_pipes() -> Callable[..., pd.DataFrame]:
    for modname in (
        "scripts.plot_hydrogen_network",
        "pypsa_eur.scripts.plot_hydrogen_network",
    ):
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        fn = getattr(mod, "group_pipes", None)
        if callable(fn):
            logger.debug("using upstream group_pipes from %s", modname)
            return fn
    return _vendored_group_pipes


group_pipes = _resolve_group_pipes()
