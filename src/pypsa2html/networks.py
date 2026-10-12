"""Lazy, cached access to solved PyPSA networks.

The legacy tool loaded *every* planning horizon into RAM up front (``load_files``,
duplicated in four scripts) and then relied on ``del n; gc.collect()`` that
freed nothing because the dict still held a reference.  This cache loads on
first use and can be bounded, so a report over six horizons no longer needs
six networks resident simultaneously.
"""

from __future__ import annotations

import gc
import logging
import re
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

#: Process-wide cache keyed by resolved ``.nc`` path.  It is the ONLY owner of
#: loaded networks: :class:`NetworkCache` instances index into it and hold none
#: themselves, so its size is the number of solved networks pypsa2html keeps in
#: RAM, across every scenario of a site.  Lets overview pages and sequential
#: scenario builds reuse networks another scenario already loaded.  Bounded by
#: ``model.network_cache_size`` through :func:`set_path_cache_max` (applied by
#: :func:`pypsa2html.build.build_site`); cleared at the end of a build.
_PATH_CACHE: OrderedDict[str, object] = OrderedDict()
_DEFAULT_PATH_CACHE_MAX = 24  # comfortably fits 6 scenarios × 4 horizons
_PATH_CACHE_MAX = _DEFAULT_PATH_CACHE_MAX


def clear_path_cache() -> None:
    """Drop every network held in the process-wide path cache."""
    if _PATH_CACHE:
        _PATH_CACHE.clear()
        _release()


def _release() -> None:
    """Free the networks just dropped from the cache, now.

    A ``pypsa.Network`` is a reference cycle (each component and accessor
    points back at the network), so dropping the cache's reference does not
    free it: it waits for the cyclic garbage collector, which runs on Python
    *object* counts, not bytes -- and a network is a few thousand objects
    holding gigabytes of arrays.  Measured on pypsa-wal (2026-10-12), bound 2:
    116 reloads left up to a dozen dead networks resident, 14.6 GB at peak.
    A full collection costs a fraction of a second, against seconds to load
    the network that replaces the evicted one.
    """
    gc.collect()


def set_path_cache_max(maxsize: int | None) -> None:
    """Bound the number of solved networks held in RAM, process-wide.

    ``None`` or a non-positive value restores the default (24).  Excess
    entries are evicted at once, oldest first.  Until 2026-10-06 the bound was
    fixed at 24 and each :class:`NetworkCache` kept its own references on top,
    so ``model.network_cache_size`` bounded nothing: a one-scenario pypsa-wal
    report over four 1 h networks (plus the other configured scenarios'
    networks) exceeded 10 GB.
    """
    global _PATH_CACHE_MAX
    if maxsize is None or int(maxsize) <= 0:
        _PATH_CACHE_MAX = _DEFAULT_PATH_CACHE_MAX
    else:
        _PATH_CACHE_MAX = int(maxsize)
    _evict()


def _evict() -> None:
    evicted_any = False
    while len(_PATH_CACHE) > _PATH_CACHE_MAX:
        evicted, _ = _PATH_CACHE.popitem(last=False)
        evicted_any = True
        logger.debug("evicted %s from the network cache", evicted)
    if evicted_any:
        _release()


def _load_network(path: Path):
    """Load ``path``, reusing a process-wide cache when possible."""
    key = str(path.resolve())
    cached = _PATH_CACHE.get(key)
    if cached is not None:
        _PATH_CACHE.move_to_end(key)
        return cached

    import pypsa  # imported here to keep `import pypsa2html` cheap

    # Make room before loading, so the bound holds at the peak too (the old
    # entry would otherwise still be resident while the new one is read), and
    # free it before reading: see _release.
    evicted_any = False
    while _PATH_CACHE and len(_PATH_CACHE) >= _PATH_CACHE_MAX:
        _PATH_CACHE.popitem(last=False)
        evicted_any = True
    if evicted_any:
        _release()
    logger.info("loading %s", path)
    network = pypsa.Network(str(path))
    _PATH_CACHE[key] = network
    return network


class NetworkCache:
    """``horizon -> pypsa.Network`` for one scenario, loaded on demand.

    An index into the process-wide cache: it holds no network itself, so the
    RAM bound is :func:`set_path_cache_max` (``model.network_cache_size``) for
    the whole process, not ``maxsize`` per scenario.  ``maxsize`` is kept for
    API compatibility.  A bound of 2 with four planning horizons reloads each
    ``.nc`` many times per build (~1–3 s per load): set it only when RAM is
    tight.
    """

    def __init__(self, paths: dict[int, Path], maxsize: int | None = None):
        self.paths = dict(paths)
        # None / non-positive → one per horizon present on disk.
        if maxsize is None or maxsize <= 0:
            self.maxsize = max(1, len(self.paths))
        else:
            self.maxsize = max(1, int(maxsize))

    @property
    def horizons(self) -> list[int]:
        return sorted(self.paths)

    def __contains__(self, horizon: int) -> bool:
        return horizon in self.paths

    def __getitem__(self, horizon: int):
        try:
            path = self.paths[horizon]
        except KeyError:
            raise KeyError(
                f"no network for horizon {horizon}; available: {self.horizons}"
            ) from None
        if not path.exists():
            raise FileNotFoundError(f"solved network not found: {path}")
        return _load_network(path)

    def first(self):
        """The earliest horizon's network -- used for node auto-detection."""
        return self[self.horizons[0]]

    def clear(self) -> None:
        """Drop this scenario's networks from the process-wide cache."""
        dropped = [_PATH_CACHE.pop(str(path.resolve()), None) for path in self.paths.values()]
        if any(net is not None for net in dropped):
            del dropped
            _release()


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


def build_cache(
    results_dir: Path,
    model_config,
    maxsize: int | None = None,
) -> NetworkCache:
    """Build a :class:`NetworkCache` for one scenario directory.

    ``maxsize`` defaults to ``model_config.network_cache_size``, then to
    ``None`` (keep all horizons).  Pass a positive int to bound RAM on
    very large multi-horizon runs.
    """
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
    if maxsize is None:
        maxsize = getattr(model_config, "network_cache_size", None)
    return NetworkCache(paths, maxsize=maxsize)
