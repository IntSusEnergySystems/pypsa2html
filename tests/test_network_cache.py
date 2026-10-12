"""``model.network_cache_size`` bounds the solved networks held in RAM.

Until 2026-10-06 it bounded nothing: the process-wide path cache kept up to 24
networks whatever the setting, and every per-scenario :class:`NetworkCache`
held its own references on top.  A one-scenario pypsa-wal report over four 1 h
networks, which also reads the other configured scenarios, was OOM-killed in a
10 GB cgroup.  ``pypsa.Network`` is replaced by a light stand-in; weak
references tell which networks are still alive.
"""

from __future__ import annotations

import gc
import weakref

import pytest

from pypsa2html import networks as nw


class _FakeNetwork:
    def __init__(self, path: str):
        self.path = path


@pytest.fixture
def fake_pypsa(monkeypatch):
    import pypsa

    loads: list[str] = []
    alive: list[weakref.ref] = []

    def _make(path):
        loads.append(path)
        net = _FakeNetwork(path)
        alive.append(weakref.ref(net))
        return net

    monkeypatch.setattr(pypsa, "Network", _make)
    nw.clear_path_cache()
    yield loads, alive
    nw.clear_path_cache()
    nw.set_path_cache_max(None)


def _paths(tmp_path, scenario, horizons=(2025, 2030, 2040, 2050)):
    out = {}
    for h in horizons:
        p = tmp_path / scenario / f"{h}.nc"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        out[h] = p
    return out


def _resident(alive) -> int:
    gc.collect()
    return sum(ref() is not None for ref in alive)


def test_bound_holds_across_scenarios(tmp_path, fake_pypsa):
    """Two scenarios, four horizons each, bound 2: never more than 2 alive."""
    _, alive = fake_pypsa
    nw.set_path_cache_max(2)
    caches = [nw.NetworkCache(_paths(tmp_path, s)) for s in ("a", "b")]
    for cache in caches:
        for h in cache.horizons:
            cache[h]  # noqa: B018 — load, keep no reference
            assert _resident(alive) <= 2
    assert len(nw._PATH_CACHE) == 2


def test_cache_holds_no_reference_of_its_own(tmp_path, fake_pypsa):
    _, alive = fake_pypsa
    nw.set_path_cache_max(1)
    cache = nw.NetworkCache(_paths(tmp_path, "a"))
    cache[2025]
    cache[2030]
    assert _resident(alive) == 1, "NetworkCache kept an evicted network alive"


def test_default_keeps_every_horizon_without_reloading(tmp_path, fake_pypsa):
    loads, _ = fake_pypsa
    nw.set_path_cache_max(None)
    cache = nw.NetworkCache(_paths(tmp_path, "a"))
    for _ in range(3):
        for h in cache.horizons:
            cache[h]
    assert len(loads) == 4
    assert cache[2025] is cache[2025]


def test_small_bound_reloads(tmp_path, fake_pypsa):
    """The trade-off, stated: a bound below the horizon count reloads."""
    loads, _ = fake_pypsa
    nw.set_path_cache_max(2)
    cache = nw.NetworkCache(_paths(tmp_path, "a"))
    for _ in range(2):
        for h in cache.horizons:
            cache[h]
    assert len(loads) == 8


def test_lowering_the_bound_evicts_at_once(tmp_path, fake_pypsa):
    _, alive = fake_pypsa
    cache = nw.NetworkCache(_paths(tmp_path, "a"))
    for h in cache.horizons:
        cache[h]
    assert _resident(alive) == 4
    nw.set_path_cache_max(1)
    assert _resident(alive) == 1


def test_clear_drops_only_this_scenario(tmp_path, fake_pypsa):
    a = nw.NetworkCache(_paths(tmp_path, "a", (2030,)))
    b = nw.NetworkCache(_paths(tmp_path, "b", (2030,)))
    a[2030], b[2030]
    a.clear()
    assert list(nw._PATH_CACHE) == [str((tmp_path / "b" / "2030.nc").resolve())]


def test_build_site_applies_and_restores_the_bound(monkeypatch, minimal_config):
    """``model.network_cache_size`` reaches the process-wide cache, then resets."""
    import yaml

    from pypsa2html import build
    from pypsa2html.config import load_config

    raw = yaml.safe_load(minimal_config.read_text())
    raw["model"]["network_cache_size"] = 2
    minimal_config.write_text(yaml.safe_dump(raw))
    seen = []
    monkeypatch.setattr(
        build, "build_scenario", lambda *a, **k: seen.append(nw._PATH_CACHE_MAX)
    )
    build.build_site(load_config(minimal_config))
    assert seen and set(seen) == {2}
    assert nw._PATH_CACHE_MAX == nw._DEFAULT_PATH_CACHE_MAX


# -- networks must be freed when evicted, not merely forgotten ----------------
#
# Measured 2026-10-12 on pypsa-wal (bound 2): 14.6 GB at peak, against 3.4 GB
# unbounded.  Two causes, each pinned below: a pypsa.Network is a reference
# cycle, freed only when the cyclic collector happens to run; and the
# per-horizon NodeResolver cached for the whole build held its network.


class _CyclicNetwork:
    """Like a pypsa.Network: its parts point back at it."""

    def __init__(self, path: str):
        self.path = path
        self.component = {"network": self}


def _alive_now(alive) -> int:
    """Live networks, WITHOUT running the collector first."""
    return sum(ref() is not None for ref in alive)


def test_an_evicted_cyclic_network_is_freed_at_once(tmp_path, monkeypatch):
    import pypsa

    alive: list[weakref.ref] = []

    def _make(path):
        net = _CyclicNetwork(path)
        alive.append(weakref.ref(net))
        return net

    monkeypatch.setattr(pypsa, "Network", _make)
    nw.clear_path_cache()
    nw.set_path_cache_max(2)
    gc.disable()     # the collector would not run on its own between loads
    try:
        cache = nw.NetworkCache(_paths(tmp_path, "a"))
        for h in cache.horizons:
            cache[h]
            assert _alive_now(alive) <= 2
    finally:
        gc.enable()
        nw.clear_path_cache()
        nw.set_path_cache_max(None)
    assert _alive_now(alive) == 0


def _mini_context(tmp_path, horizons=(2025, 2030, 2040, 2050)):
    """A real BuildContext over copies of tests/data/mini.nc, one per horizon."""
    import shutil
    from pathlib import Path

    import yaml

    from pypsa2html.config import load_config
    from pypsa2html.context import BuildContext
    from pypsa2html.datafiles import load_taxonomy
    from pypsa2html.nodes import build_node_set

    mini = Path(__file__).parent / "data" / "mini.nc"
    if not mini.exists():
        pytest.skip("tests/data/mini.nc missing")
    results = tmp_path / "results" / "demo"
    (results / "networks").mkdir(parents=True)
    paths = {}
    for h in horizons:
        paths[h] = results / "networks" / f"base_s_adm___{h}.nc"
        shutil.copy(mini, paths[h])
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "root": str(tmp_path),
        "scenarios": [{"name": "demo", "results_dir": "results/demo"}],
    }))
    cfg = load_config(cfg_path)
    return BuildContext(
        config=cfg, scenario=cfg.scenario("demo"), taxonomy=load_taxonomy(),
        nodes=build_node_set(["AA", "BB"], focus="AA"),
        networks=nw.NetworkCache(paths), results_dir=results, resources_dir=results,
    )


def test_a_cached_resolver_does_not_pin_its_network(tmp_path, monkeypatch):
    pypsa = pytest.importorskip("pypsa")
    real = pypsa.Network
    alive: list[weakref.ref] = []

    def _tracked(path):
        net = real(path)
        alive.append(weakref.ref(net))
        return net

    monkeypatch.setattr(pypsa, "Network", _tracked)
    nw.clear_path_cache()
    nw.set_path_cache_max(2)
    try:
        ctx = _mini_context(tmp_path)
        for h in ctx.horizons:
            ctx.networks[h]
            assert len(ctx.resolver(h).select_locations("generators", ["AA"])) == 1
        gc.collect()
        assert _alive_now(alive) <= 2, "cached resolvers kept evicted networks alive"

        # An evicted horizon's resolver still answers: it reloads through the cache.
        assert list(ctx.resolver(2025).select_locations("generators", ["AA"])) == ["AA solar"]
        assert ctx.resolver(2025).n is ctx.networks[2025]
    finally:
        nw.clear_path_cache()
        nw.set_path_cache_max(None)


def test_a_resolver_built_directly_keeps_its_network():
    """Without a reload callback nothing changes: the resolver owns the network."""
    from types import SimpleNamespace

    import pandas as pd

    from pypsa2html.nodes import NodeResolver

    net = SimpleNamespace(buses=pd.DataFrame({"location": ["AA"]}, index=["AA"]))
    resolver = NodeResolver(net)
    assert resolver.n is net
