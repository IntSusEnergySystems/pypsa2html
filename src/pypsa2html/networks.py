"""Lazy, cached access to solved PyPSA networks.

The legacy tool loaded *every* planning horizon into RAM up front (``load_files``,
duplicated in four scripts) and then relied on ``del n; gc.collect()`` that
freed nothing because the dict still held a reference.  This cache loads on
first use and can be bounded, so a report over six horizons no longer needs
six networks resident simultaneously.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)


class NetworkCache:
    """``horizon -> pypsa.Network``, loaded on demand, LRU-bounded."""

    def __init__(self, paths: dict[int, Path], maxsize: int = 2):
        self.paths = dict(paths)
        self.maxsize = max(1, maxsize)
        self._cache: OrderedDict[int, object] = OrderedDict()

    @property
    def horizons(self) -> list[int]:
        return sorted(self.paths)

    def __contains__(self, horizon: int) -> bool:
        return horizon in self.paths

    def __getitem__(self, horizon: int):
        if horizon in self._cache:
            self._cache.move_to_end(horizon)
            return self._cache[horizon]
        try:
            path = self.paths[horizon]
        except KeyError:
            raise KeyError(
                f"no network for horizon {horizon}; available: {self.horizons}"
            ) from None
        if not path.exists():
            raise FileNotFoundError(f"solved network not found: {path}")

        import pypsa  # imported here to keep `import pypsa2html` cheap

        logger.info("loading %s", path)
        network = pypsa.Network(str(path))
        self._cache[horizon] = network
        while len(self._cache) > self.maxsize:
            evicted, _ = self._cache.popitem(last=False)
            logger.debug("evicted network for horizon %s from cache", evicted)
        return network

    def first(self):
        """The earliest horizon's network -- used for node auto-detection."""
        return self[self.horizons[0]]

    def clear(self) -> None:
        self._cache.clear()


def discover_horizons(results_dir: Path, pattern: str, **fmt) -> list[int]:
    """Find the planning horizons present on disk for one scenario.

    ``pattern`` is the config's ``model.network_pattern`` with a ``{horizon}``
    placeholder; the other placeholders are filled from ``fmt``.  Everything
    else in the name is matched literally, so a stray ``base_s_adm___2020.nc``
    left over from an unrelated run is only picked up if it really matches.
    """
    literal = pattern.format(horizon="\x00", **fmt)
    head, _, tail = literal.partition("\x00")
    regex = re.compile(re.escape(head) + r"(\d{4})" + re.escape(tail) + r"$")

    horizons = []
    for path in sorted(results_dir.rglob("*.nc")):
        rel = path.relative_to(results_dir).as_posix()
        match = regex.match(rel)
        if match:
            horizons.append(int(match.group(1)))
    return sorted(set(horizons))


def build_cache(results_dir: Path, model_config, maxsize: int = 2) -> NetworkCache:
    """Build a :class:`NetworkCache` for one scenario directory."""
    fmt = {
        "clusters": model_config.clusters,
        "opts": model_config.opts,
        "sector_opts": model_config.sector_opts,
    }
    horizons = model_config.planning_horizons
    if not horizons:
        horizons = discover_horizons(results_dir, model_config.network_pattern, **fmt)
        if not horizons:
            raise FileNotFoundError(
                f"no networks matching {model_config.network_pattern!r} under "
                f"{results_dir}. Set model.planning_horizons explicitly, or check "
                "model.clusters / model.opts / model.sector_opts."
            )
        logger.info("discovered planning horizons %s in %s", horizons, results_dir)

    paths = {h: model_config.network_path(results_dir, h) for h in horizons}
    missing = {h: p for h, p in paths.items() if not p.exists()}
    if missing:
        raise FileNotFoundError(
            "configured planning horizons have no solved network: "
            + ", ".join(f"{h} -> {p}" for h, p in missing.items())
        )
    return NetworkCache(paths, maxsize=maxsize)
