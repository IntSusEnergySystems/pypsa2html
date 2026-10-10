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
