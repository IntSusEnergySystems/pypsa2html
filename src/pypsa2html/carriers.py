"""PyPSA carrier naming conventions shared by extraction and charts.

Capture-equipped siblings in the PyPSA-Eur family are named by appending
`` CC`` (sometimes `` CCS``) to the unabated carrier -- ``CCGT CC``,
``SMR CC``, ``coal CC``, ``urban central gas CHP CC``.  The report treats
that suffix as a *convention*, not as a list of technologies, so an ad-hoc
plant added in one model (a natural-gas CCGT with post-combustion capture)
shows up without a Python special case.

See docs/DESIGN_DECISIONS.md D17.
"""

from __future__ import annotations

import re

import pandas as pd

#: Trailing capture suffix used by PyPSA-Eur and its forks.
_CCS_SUFFIX = re.compile(r"^(?P<base>.+?)(?: CC| CCS)$")

#: Rank suffix produced by :func:`pypsa2html.extract.flows._rank_entries`
#: and :func:`pypsa2html.extract.emissions._entry_names` (``CCGT_2``).
_RANK_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<rank>\d+)$")


def ccs_parent_carrier(name: str) -> str | None:
    """Return the unabated sibling of a ``{tech} CC`` carrier, or ``None``."""
    match = _CCS_SUFFIX.fullmatch(str(name).strip())
    if match is None:
        return None
    base = match.group("base")
    return base if base else None


def inherit_ccs_entry(entry: str) -> str | None:
    """Map ``CCGT CC`` / ``CCGT CC_2`` onto ``CCGT`` / ``CCGT_2``.

    ``None`` when ``entry`` is not a capture variant.  The caller decides
    whether the parent actually exists in the taxonomy.
    """
    raw = str(entry)
    rank = None
    base = raw
    ranked = _RANK_SUFFIX.fullmatch(raw)
    if ranked is not None:
        base = ranked.group("base")
        rank = ranked.group("rank")
    parent = ccs_parent_carrier(base)
    if parent is None:
        return None
    return f"{parent}_{rank}" if rank else parent


def fold_ccs_variants(series: pd.Series, known_entries: set[str]) -> pd.Series:
    """Fold unmapped ``{tech} CC`` rows onto the parent taxonomy entry.

    Entries already present in ``known_entries`` are left alone, so an
    explicit ``SMR CC`` row in ``carrier_flows_*.csv`` still wins.  Child
    rows that fold onto a parent are dropped so they are not logged as
    unknown.
    """
    if series is None or series.empty:
        return series
    out = series.copy()
    drop: list = []
    extras: dict[str, float] = {}
    for entry, value in series.items():
        key = str(entry)
        if key in known_entries:
            continue
        parent = inherit_ccs_entry(key)
        if parent is None or parent not in known_entries:
            continue
        extras[parent] = extras.get(parent, 0.0) + float(value)
        drop.append(entry)
    if not extras and not drop:
        return out
    for parent, extra in extras.items():
        current = float(out[parent]) if parent in out.index else 0.0
        out[parent] = current + extra
    if drop:
        out = out.drop(labels=drop)
    return out
