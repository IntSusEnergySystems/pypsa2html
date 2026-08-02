"""Dispatch window derivation and builder registration."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.build import resolve_builder
from pypsa2html.config import DispatchWindowsConfig, ModelConfig
from pypsa2html.extract.balance import derive_dispatch_window, dispatch_window


def test_derive_winter_window_from_snapshots():
    snapshots = pd.date_range("2018-01-01", periods=8760, freq="h")
    start, stop = derive_dispatch_window(snapshots, "winter")
    assert start.month == 2
    assert start.day == 8
    assert (stop - start).days == 6


def test_derive_summer_window_from_snapshots():
    snapshots = pd.date_range("2025-01-01", periods=8760, freq="h")
    start, stop = derive_dispatch_window(snapshots, "summer")
    assert start.month == 7
    assert start.day == 1
    assert (stop - start).days == 6


def test_derive_window_empty_index_returns_none():
    assert derive_dispatch_window(pd.DatetimeIndex([]), "winter") is None


def test_dispatch_window_uses_config_when_set():
    model = ModelConfig(
        dispatch_windows=DispatchWindowsConfig(
            winter=["2030-02-01", "2030-02-07"],
            summer=None,
        )
    )
    ctx = SimpleNamespace(config=SimpleNamespace(model=model))
    start, stop = dispatch_window(ctx, "winter")
    assert start == pd.Timestamp("2030-02-01")
    assert stop == pd.Timestamp("2030-02-07")


@pytest.mark.parametrize(
    "builder",
    [
        "dispatch.power_winter",
        "dispatch.power_summer",
        "dispatch.heat_winter",
        "dispatch.heat_summer",
    ],
)
def test_dispatch_builders_resolve(builder):
    assert resolve_builder(builder) is not None
