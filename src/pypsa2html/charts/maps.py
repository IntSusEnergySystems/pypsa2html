"""Geographic map pages: system costs, hydrogen and gas networks.

Maps are optional (``pip install pypsa2html[maps]``).  When matplotlib,
geopandas or cartopy is missing, or inputs are absent, builders log a warning
and return ``None`` rather than failing the build.

Each horizon is rendered once; PNGs are written next to the HTML output and
referenced with ``<img src=...>`` — never base64-embedded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .base import Html

if TYPE_CHECKING:
    from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

_MAPS_AVAILABLE = False
_IMPORT_ERROR: Exception | None = None

try:
    import cartopy.crs as ccrs
    import geopandas as gpd
    import matplotlib.pyplot as plt

    _MAPS_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - exercised when [maps] not installed
    _IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def costs(ctx, node: str, section) -> Html | None:
    """Choropleth of nodal capital cost / GDP plus transmission capacity."""
    return _build_maps_page(ctx, section, _render_costs_map)


def hydrogen(ctx, node: str, section) -> Html | None:
    """Hydrogen pipelines and storage."""
    return _build_maps_page(ctx, section, _render_hydrogen_map)


def gas(ctx, node: str, section) -> Html | None:
    """Gas pipelines."""
    return _build_maps_page(ctx, section, _render_gas_map)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def _build_maps_page(ctx, section, render: Callable) -> Html | None:
    if not _maps_available():
        return None
    if not hasattr(ctx, "networks") or not hasattr(ctx, "scenario"):
        logger.warning("maps require a BuildContext with solved networks")
        return None

    regions = _load_regions(ctx)
    if regions is None:
        return None

    out_dir = ctx.config.output_dir(ctx.scenario.name)
    out_dir.mkdir(parents=True, exist_ok=True)

    images: list[tuple[int, str]] = []
    for horizon in ctx.horizons:
        try:
            network = ctx.networks[horizon]
        except (KeyError, FileNotFoundError) as exc:
            logger.warning("no network for horizon %s: %s", horizon, exc)
            continue

        fig = render(network, regions, ctx)
        if fig is None:
            continue

        filename = f"map_{section.id}_{horizon}.png"
        path = out_dir / filename
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        images.append((horizon, filename))
        logger.info("wrote map %s", path)

    if not images:
        return None
    return Html(_horizon_fragment(section.title, images))


def _horizon_fragment(title: str, images: list[tuple[int, str]]) -> str:
    """Tab strip HTML for one map section (fragment only, no document wrapper)."""
    tab_id = title.lower().replace(" ", "_").replace("-", "_")
    parts = ['<div class="p2h-maps">']
    if len(images) > 1:
        parts.append('<div class="p2h-map-tabs">')
        for i, (horizon, _) in enumerate(images):
            active = " active" if i == 0 else ""
            parts.append(
                f'<button type="button" class="p2h-map-tab{active}" '
                f'data-p2h-map-target="{tab_id}_{horizon}">{horizon}</button>'
            )
        parts.append("</div>")
        parts.append(
            "<script>"
            "(function(){"
            "document.querySelectorAll('.p2h-map-tab').forEach(function(btn){"
            "btn.addEventListener('click',function(){"
            "var root=btn.closest('.p2h-maps');"
            "root.querySelectorAll('.p2h-map-panel').forEach(function(p){"
            "p.style.display='none';});"
            "root.querySelectorAll('.p2h-map-tab').forEach(function(b){"
            "b.classList.remove('active');});"
            "root.querySelector('#'+btn.dataset.p2hMapTarget).style.display='block';"
            "btn.classList.add('active');});});})();"
            "</script>"
        )

    for i, (horizon, filename) in enumerate(images):
        display = "block" if i == 0 else "none"
        panel_style = "" if len(images) == 1 else f' style="display:{display}"'
        parts.append(
            f'<div id="{tab_id}_{horizon}" class="p2h-map-panel"{panel_style}>'
            f"<h3>{horizon}</h3>"
            f'<img src="{filename}" alt="{title} {horizon}" loading="lazy">'
            f"</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Geojson and GDP
# ---------------------------------------------------------------------------

def _geojson_path(ctx) -> Path | None:
    clusters = getattr(ctx.config.model, "clusters", "adm")
    path = Path(ctx.resources_dir) / f"regions_onshore_base_s_{clusters}.geojson"
    if not path.exists():
        logger.warning("regions geojson not found: %s", path)
        return None
    return path


def _load_regions(ctx) -> gpd.GeoDataFrame | None:
    if not _MAPS_AVAILABLE:
        return None
    path = _geojson_path(ctx)
    if path is None:
        return None

    key = ("geojson", str(path))
    cached = ctx._files.get(key)
    if cached is not None:
        return cached

    gdf = gpd.read_file(path)
    name_col = _region_name_column(gdf)
    if name_col is None:
        logger.warning("geojson %s has no name/NAME property", path)
        ctx._files[key] = None
        return None
    gdf = gdf.set_index(name_col)
    gdf = gdf.to_crs(ccrs.EqualEarth())
    ctx._files[key] = gdf
    return gdf


def _region_name_column(gdf: gpd.GeoDataFrame) -> str | None:
    for col in ("name", "NAME"):
        if col in gdf.columns:
            return col
    return None


def _gdp_bneur(ctx) -> pd.Series | None:
    regions = ctx.taxonomy.regions
    if "gdp_bneur" not in regions.columns:
        logger.warning("regions.csv has no gdp_bneur column; skipping cost/GDP map")
        return None
    gdp = pd.to_numeric(regions["gdp_bneur"], errors="coerce")
    return gdp


# ---------------------------------------------------------------------------
# Plotting helpers
# ---------------------------------------------------------------------------

def _map_figure() -> tuple[Figure, object, ccrs.CRS]:
    proj = ccrs.EqualEarth()
    fig, ax = plt.subplots(figsize=(12, 12), subplot_kw={"projection": proj})
    ax.set_extent([-12, 32, 35, 72], crs=ccrs.PlateCarree())
    ax.coastlines(resolution="50m", linewidth=0.5)
    ax.set_facecolor("white")
    return fig, ax, proj


def _nodal_costs_beur(network) -> pd.Series:
    """Annualised capital cost per bus location, in bEUR/year."""
    totals = pd.Series(0.0, dtype=float)
    for comp in ("generators", "links", "stores", "storage_units"):
        df = getattr(network, comp)
        if df.empty:
            continue
        attr = "e_nom_opt" if comp == "stores" else "p_nom_opt"
        if attr not in df.columns or "capital_cost" not in df.columns:
            continue
        if "location" not in df.columns:
            continue
        annual = df["capital_cost"].fillna(0) * df[attr].fillna(0)
        totals = totals.add(annual.groupby(df["location"]).sum(), fill_value=0.0)
    return totals / 1e9


def _cost_gdp_ratio(costs: pd.Series, gdp: pd.Series) -> pd.Series:
    ratio = pd.Series(dtype=float)
    for loc, cost in costs.items():
        if loc not in gdp.index:
            continue
        denom = gdp.loc[loc]
        if pd.isna(denom) or denom == 0:
            continue
        ratio[loc] = float(cost) / float(denom) * 100.0
    return ratio


def _draw_branch(
    network,
    ax,
    *,
    component: str,
    mask: pd.Series,
    color: str,
    scale: float,
    lower_threshold: float = 0.0,
    transform=None,
) -> None:
    buses = network.buses
    df = getattr(network, component)
    if df.empty:
        return
    subset = df.loc[mask]
    for _, row in subset.iterrows():
        cap = row.get("s_nom_opt") if component == "lines" else row.get("p_nom_opt")
        if cap is None or cap < lower_threshold:
            continue
        try:
            x0, y0 = buses.at[row.bus0, "x"], buses.at[row.bus0, "y"]
            x1, y1 = buses.at[row.bus1, "x"], buses.at[row.bus1, "y"]
        except KeyError:
            continue
        lw = max(float(cap) / scale, 0.2)
        ax.plot(
            [x0, x1], [y0, y1],
            color=color,
            linewidth=lw,
            solid_capstyle="round",
            transform=transform or ccrs.PlateCarree(),
            zorder=2,
        )


def _render_costs_map(network, regions: gpd.GeoDataFrame, ctx) -> Figure | None:
    gdp = _gdp_bneur(ctx)
    if gdp is None:
        return None

    costs = _nodal_costs_beur(network)
    ratio = _cost_gdp_ratio(costs, gdp)
    if ratio.empty:
        logger.warning("no cost/GDP ratios to plot (missing or zero GDP entries)")
        return None

    fig, ax, _ = _map_figure()
    plot_regions = regions.copy()
    plot_regions["cost_pct_gdp"] = plot_regions.index.map(ratio)
    plot_regions.plot(
        ax=ax,
        column="cost_pct_gdp",
        cmap="Greys",
        linewidth=0,
        legend=True,
        vmin=0,
        vmax=6,
        missing_kwds={"color": "lightgrey", "hatch": "///"},
        legend_kwds={"label": "Cost [% GDP / year]", "shrink": 0.6},
    )

    line_mask = pd.Series(True, index=network.lines.index)
    _draw_branch(
        network, ax, component="lines", mask=line_mask,
        color="#9a0200", scale=1e3, lower_threshold=500.0,
    )
    if not network.links.empty:
        dc_mask = network.links.carrier.isin(["DC", "B2B"])
        _draw_branch(
            network, ax, component="links", mask=dc_mask,
            color="#11875d", scale=1e3, lower_threshold=500.0,
        )

    ax.set_title("System cost map")
    fig.tight_layout()
    return fig


def _render_hydrogen_map(network, regions: gpd.GeoDataFrame, ctx) -> Figure | None:
    if network.links.empty:
        logger.warning("hydrogen map: network has no links")
        return None

    h2_links = network.links.carrier.str.contains("H2 pipeline", na=False)
    if not h2_links.any():
        logger.warning("hydrogen map: no H2 pipeline links in network")
        return None

    fig, ax, _ = _map_figure()

    if not network.stores.empty:
        h2_store = network.stores.loc[network.stores.carrier == "H2"]
        if not h2_store.empty and "location" in h2_store.columns:
            storage_twh = (
                h2_store["e_nom_opt"].groupby(h2_store["location"]).sum() / 1e6
            )
            plot_regions = regions.copy()
            plot_regions["H2_TWh"] = plot_regions.index.map(storage_twh)
            plot_regions.plot(
                ax=ax,
                column="H2_TWh",
                cmap="Blues",
                linewidth=0,
                legend=True,
                vmin=0,
                vmax=6,
                legend_kwds={"label": "H2 storage [TWh]", "shrink": 0.6},
            )

    _draw_branch(
        network, ax, component="links", mask=h2_links,
        color="#11875d", scale=7e3, lower_threshold=750.0,
    )
    ax.set_title("Hydrogen network")
    fig.tight_layout()
    return fig


def _render_gas_map(network, regions: gpd.GeoDataFrame, ctx) -> Figure | None:
    if network.links.empty:
        logger.warning("gas map: network has no links")
        return None

    gas_links = network.links.carrier.str.contains("gas pipeline", na=False)
    if not gas_links.any():
        logger.warning("gas map: no gas pipeline links in network")
        return None

    fig, ax, _ = _map_figure()

    # Reposition EU gas for display only — never mutate the cached network.
    buses = network.buses
    x = buses["x"].copy()
    y = buses["y"].copy()
    if "EU gas" in buses.index:
        x.loc["EU gas"] = -50.0
        y.loc["EU gas"] = 46.0

    def _xy(bus_name: str) -> tuple[float, float] | None:
        for candidate in (bus_name, str(bus_name).replace(" gas", "")):
            if candidate in x.index:
                return float(x.at[candidate]), float(y.at[candidate])
        return None

    subset = network.links.loc[gas_links]
    for _, row in subset.iterrows():
        cap = row.get("p_nom_opt")
        if cap is None or cap < 1e3:
            continue
        start = _xy(row.bus0)
        end = _xy(row.bus1)
        if start is None or end is None:
            continue
        x0, y0 = start
        x1, y1 = end
        lw = max(float(cap) / 5e3, 0.2)
        ax.plot(
            [x0, x1], [y0, y1],
            color="#c14a09",
            linewidth=lw,
            solid_capstyle="round",
            transform=ccrs.PlateCarree(),
            zorder=2,
        )

    regions.plot(ax=ax, facecolor="none", edgecolor="lightgrey", linewidth=0.3)
    ax.set_title("Gas network")
    fig.tight_layout()
    return fig


def _maps_available() -> bool:
    if not _MAPS_AVAILABLE:
        logger.warning(
            "maps extras not installed (pip install pypsa2html[maps]): %s",
            _IMPORT_ERROR,
        )
        return False
    return True
