"""Geographic map pages: system costs, hydrogen and gas networks.

Maps are optional (``pip install pypsa2html[maps]``).  When matplotlib,
geopandas or cartopy is missing, or inputs are absent, builders log a warning
and return ``None`` rather than failing the build.

Each horizon is rendered once; PNGs are written next to the HTML output and
referenced with ``<img src=...>`` — never base64-embedded.  Across scenarios,
identical map inputs reuse a previously written PNG via hardlink/copy.

Rendering follows the legacy ``plot_map`` / ``plot_h2_map`` / ``plot_ch4_map``
layout (PyPSA pie charts at buses, choropleth, legends) but uses the current
``n.plot`` API (``bus_size`` / ``line_width`` / …) and never mutates the
cached network.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .base import Html, apply_tech_map, resolve_tech_color, tech_color_map

if TYPE_CHECKING:
    from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

_MAPS_AVAILABLE = False
_IMPORT_ERROR: Exception | None = None

try:
    import cartopy.crs as ccrs
    import geopandas as gpd
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from pypsa.plot import add_legend_circles, add_legend_lines, add_legend_patches

    _MAPS_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - exercised when [maps] not installed
    _IMPORT_ERROR = exc

#: Process-wide map of content fingerprint → first PNG path written this run.
_MAP_PNG_BY_FINGERPRINT: dict[str, Path] = {}

#: Europe extent used by the legacy plotting config.
_MAP_BOUNDARIES = (-11.0, 30.0, 34.0, 71.0)

#: PNG resolution — high enough for HTML, without the 2 MB base64 embeds.
_MAP_DPI = 150
_MAP_FIGSIZE = (15, 15)

_AC_COLOR = "#9a0200"
_DC_COLOR = "#11875d"
_COST_BUS_SIZE_FACTOR = 35e9
_COST_LINE_LOWER = 500.0
_COST_LINE_UPPER = 1e4
_COST_LINEWIDTH_FACTOR = 1e3
_COST_THRESHOLD = 100e6  # EUR/a — techs below this leave the legend


def clear_map_png_cache() -> None:
    """Drop cross-scenario PNG reuse state (called at the start of a site build)."""
    _MAP_PNG_BY_FINGERPRINT.clear()


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def costs(ctx, node: str, section) -> Html | None:
    """Choropleth of nodal capital cost / GDP plus technology pie charts."""
    return _build_maps_page(ctx, section, _render_costs_map, _fingerprint_costs, node)


def hydrogen(ctx, node: str, section) -> Html | None:
    """Hydrogen pipelines and storage."""
    return _build_maps_page(
        ctx, section, _render_hydrogen_map, _fingerprint_hydrogen, node
    )


def gas(ctx, node: str, section) -> Html | None:
    """Gas pipelines and supply sources."""
    return _build_maps_page(ctx, section, _render_gas_map, _fingerprint_gas, node)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def _build_maps_page(
    ctx,
    section,
    render: Callable,
    fingerprint_fn: Callable,
    node: str,
) -> Html | None:
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
    highlight = _highlight_node(ctx, node)

    images: list[tuple[int, str]] = []
    for horizon in ctx.horizons:
        try:
            network = ctx.networks[horizon]
        except (KeyError, FileNotFoundError) as exc:
            logger.warning("no network for horizon %s: %s", horizon, exc)
            continue

        filename = f"map_{section.id}_{horizon}.png"
        if highlight:
            filename = f"map_{section.id}_{horizon}_{highlight}.png"
        path = out_dir / filename

        fp = fingerprint_fn(network, regions, ctx, highlight=highlight)
        if fp and _reuse_map_png(fp, path):
            images.append((horizon, filename))
            logger.info("reused map %s", path)
            continue

        fig = render(network, regions, ctx, highlight=highlight)
        if fig is None:
            continue

        fig.savefig(path, dpi=_MAP_DPI, bbox_inches="tight")
        plt.close(fig)
        if fp:
            _MAP_PNG_BY_FINGERPRINT[fp] = path
        images.append((horizon, filename))
        logger.info("wrote map %s", path)

    if not images:
        return None
    return Html(_horizon_fragment(section.title, images))


def _highlight_node(ctx, node: str) -> str | None:
    """Region code to outline when maps are per-node; ``None`` for shared maps."""
    if getattr(ctx.config.output, "shared_maps", True):
        return None
    if hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node):
        return None
    return node or None


def _reuse_map_png(fingerprint: str, dest: Path) -> bool:
    """Copy/hardlink a previously rendered PNG with the same inputs."""
    src = _MAP_PNG_BY_FINGERPRINT.get(fingerprint)
    if src is None or not src.is_file():
        return False
    if src.resolve() == dest.resolve():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
    return True


def _hash_parts(*parts) -> str:
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            h.update(b"\x00")
        elif isinstance(part, pd.Series):
            series = pd.to_numeric(part, errors="coerce").astype(float).round(6)
            series.index = series.index.map(str)
            series = series.sort_index()
            h.update(series.to_csv(header=False).encode())
        elif isinstance(part, pd.DataFrame):
            frame = part.copy()
            for col in frame.columns:
                if pd.api.types.is_numeric_dtype(frame[col]):
                    frame[col] = (
                        pd.to_numeric(frame[col], errors="coerce").astype(float).round(6)
                    )
            frame.index = frame.index.map(str)
            frame = frame.sort_index().sort_index(axis=1)
            h.update(frame.to_csv().encode())
        else:
            h.update(repr(part).encode())
    return h.hexdigest()


def _fingerprint_costs(network, regions, ctx, *, highlight: str | None) -> str | None:
    gdp = _gdp_bneur(ctx)
    if gdp is None:
        return None
    costs = _tech_costs_by_bus(network)
    if costs is None or costs.empty:
        return None
    ratio = _cost_gdp_ratio(costs.groupby(level=0).sum() / 1e9, gdp).round(2)
    line_caps = (
        network.lines["s_nom_opt"]
        if not network.lines.empty and "s_nom_opt" in network.lines.columns
        else pd.Series(dtype=float)
    )
    link_caps = pd.Series(dtype=float)
    if not network.links.empty and "p_nom_opt" in network.links.columns:
        dc = network.links.carrier.isin(["DC", "B2B"])
        link_caps = network.links.loc[dc, "p_nom_opt"]
    line_caps = line_caps[line_caps >= _COST_LINE_LOWER] if len(line_caps) else line_caps
    link_caps = link_caps[link_caps >= _COST_LINE_LOWER] if len(link_caps) else link_caps
    return _hash_parts("costs", costs.round(-4), ratio, line_caps, link_caps, highlight)


def _fingerprint_hydrogen(network, regions, ctx, *, highlight: str | None) -> str | None:
    if network.links.empty:
        return None
    h2 = network.links.carrier.str.contains("H2 pipeline", na=False)
    if not h2.any():
        return None
    pipes = network.links.loc[h2, ["bus0", "bus1", "p_nom_opt", "carrier"]]
    pipes = pipes.loc[pipes["p_nom_opt"] >= 750.0]
    storage = pd.Series(dtype=float)
    if not network.stores.empty:
        h2_store = network.stores.loc[network.stores.carrier == "H2"]
        if not h2_store.empty and "location" in h2_store.columns:
            storage = h2_store["e_nom_opt"].groupby(h2_store["location"]).sum()
    elec = pd.Series(dtype=float)
    if not network.links.empty:
        mask = network.links.carrier == "H2 Electrolysis"
        if mask.any():
            elec = network.links.loc[mask, "p_nom_opt"]
    return _hash_parts("hydrogen", pipes, storage, elec, highlight)


def _fingerprint_gas(network, regions, ctx, *, highlight: str | None) -> str | None:
    if network.links.empty:
        return None
    gas = network.links.carrier.str.contains("gas pipeline", na=False)
    if not gas.any():
        return None
    pipes = network.links.loc[gas, ["bus0", "bus1", "p_nom_opt", "carrier"]]
    pipes = pipes.loc[pipes["p_nom_opt"] >= 1e3]
    return _hash_parts("gas", pipes, highlight)


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
    return pd.to_numeric(regions["gdp_bneur"], errors="coerce")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _map_opts() -> dict:
    # auto_scale_branches=True → LineCollection (matplotlib linewidths).
    # False draws PatchCollection polygons scaled by the projection
    # area_factor, which blows up into map-covering blobs on EqualEarth.
    return {
        "boundaries": _MAP_BOUNDARIES,
        "geomap": True,
        "geomap_color": {"ocean": "white", "land": "white"},
        "auto_scale_branches": True,
    }


def _map_colors() -> dict[str, str]:
    colors = tech_color_map()
    # Aliases used by the legacy map legend / pie slices.
    aliases = {
        "distribution network": colors.get(
            "electricity distribution grid", colors.get("distribution network", "#97ad8c")
        ),
        "thermal energy storage": colors.get(
            "hot water storage", colors.get("thermal energy storage", "#eab88b")
        ),
        "H2 storage": colors.get("H2", "#bf13a0"),
        "TES & H2 storage": colors.get("TES & H2 storage", "#870c71"),
        "CCUS": colors.get("CCS", colors.get("CCUS", "#f29dae")),
        "CCU": colors.get("CCS", colors.get("CCU", "#f29dae")),
        "Fossil fuel powerplants": colors.get(
            "Fossil fuels & powerplants", "#e05b09"
        ),
        "Fossil fuels & powerplants": colors.get(
            "Fossil fuels & powerplants", "#e05b09"
        ),
        "boilers": colors.get("boilers", "#e05b09"),
        "battery storage": colors.get("battery storage", colors.get("battery", "#ace37f")),
        "fossil gas": "#9a0200",
        "methanation": "#ffbacd",
        "biogas": "#32bf84",
        "H2 Electrolysis": "#ffbacd",
    }
    colors.update({k: v for k, v in aliases.items() if v})
    return colors


def _assign_locations(n) -> None:
    """Fill component ``location`` from bus locations (in-place on a copy).

    Solved networks often ship an empty-string ``location`` column on
    generators/links; treating that as "already set" leaves every pie at the
    unlocatable ``''`` bus.  Always overwrite from ``n.buses.location``.
    """
    if "location" not in n.buses.columns:
        return
    for c in n.iterate_components(n.one_port_components):
        c.df["location"] = c.df.bus.map(n.buses.location)
    for c in n.iterate_components(n.branch_components):
        bus_cols = c.df.filter(regex="^bus")
        locs = bus_cols.apply(lambda col: col.map(n.buses.location)).sort_index(axis=1)
        c.df["location"] = locs.apply(
            lambda row: next(
                (loc for loc in row.dropna() if loc != "EU"),
                "EU",
            ),
            axis=1,
        )


def _drop_non_ac_buses(n) -> None:
    if n.buses.empty or "carrier" not in n.buses.columns:
        return
    drop = n.buses.index[n.buses.carrier != "AC"]
    if len(drop):
        n.buses.drop(drop, inplace=True)


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


def _tech_costs_by_bus(network) -> pd.Series | None:
    """Annualised capital cost MultiIndex (bus/location, tech) in EUR/year.

    Locations are taken from ``n.buses.location`` via each component's bus
    column — the on-disk ``location`` attribute is often an empty string.
    """
    n = network
    bus_loc = n.buses["location"] if "location" in n.buses.columns else None
    if bus_loc is None:
        return None

    frames: list[pd.DataFrame] = []
    for comp in ("generators", "links", "stores", "storage_units"):
        df = getattr(n, comp)
        if df.empty:
            continue
        attr = "e_nom_opt" if comp == "stores" else "p_nom_opt"
        if attr not in df.columns or "capital_cost" not in df.columns:
            continue
        if "carrier" not in df.columns:
            continue
        if comp == "links":
            # Prefer a non-EU endpoint so cross-border links land on a real bus.
            bus_cols = df.filter(regex="^bus")
            locs = bus_cols.apply(lambda col: col.map(bus_loc))
            location = locs.apply(
                lambda row: next(
                    (loc for loc in row.dropna() if loc != "EU"),
                    "EU",
                ),
                axis=1,
            )
        else:
            location = df.bus.map(bus_loc)
        nice = apply_tech_map(df["carrier"].astype(str), "map")
        nice = nice.replace({"Fossil fuels & powerplants": "Fossil fuel powerplants"})
        annual = df["capital_cost"].fillna(0) * df[attr].fillna(0)
        costs_c = (
            annual.groupby([location, nice])
            .sum()
            .unstack(fill_value=0.0)
        )
        frames.append(costs_c)
    if not frames:
        return None
    costs = pd.concat(frames, axis=1)
    costs = costs.T.groupby(costs.columns).sum().T
    costs = costs.loc[:, (costs != 0.0).any(axis=0)]
    if costs.empty:
        return None
    return costs.stack()


def _outline_highlight(ax, regions: gpd.GeoDataFrame, highlight: str | None) -> None:
    if not highlight or highlight not in regions.index:
        return
    regions.loc[[highlight]].plot(
        ax=ax,
        facecolor="none",
        edgecolor="#c0392b",
        linewidth=2.0,
        zorder=5,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_costs_map(
    network, regions: gpd.GeoDataFrame, ctx, *, highlight: str | None = None
) -> Figure | None:
    gdp = _gdp_bneur(ctx)
    if gdp is None:
        return None

    costs = _tech_costs_by_bus(network)
    if costs is None or costs.empty:
        logger.warning("no technology costs to plot on the map")
        return None

    n = network.copy()
    _assign_locations(n)
    _drop_non_ac_buses(n)

    # Keep only costs whose location matches an AC bus.
    to_drop = costs.index.levels[0].difference(n.buses.index)
    if len(to_drop):
        costs = costs.drop(to_drop, level=0, errors="ignore")
    if costs.empty:
        logger.warning("cost map: no costs left after aligning to AC buses")
        return None
    costs.index = pd.MultiIndex.from_tuples(costs.index.values)

    combined = costs.groupby(level=0).sum() / 1e9
    ratio = _cost_gdp_ratio(combined, gdp)
    if ratio.empty:
        logger.warning("no cost/GDP ratios to plot (missing or zero GDP entries)")
        return None

    carriers = costs.groupby(level=1).sum()
    carriers = carriers.where(carriers > _COST_THRESHOLD).dropna()
    carrier_list = list(carriers.index)

    # Transmission: keep AC lines + DC/B2B links only.
    if not n.links.empty:
        keep = n.links.carrier.isin(["DC", "B2B"])
        n.links.drop(n.links.index[~keep], inplace=True)

    line_widths = (
        n.lines["s_nom_opt"] if not n.lines.empty else pd.Series(dtype=float)
    )
    link_widths = (
        n.links["p_nom_opt"] if not n.links.empty else pd.Series(dtype=float)
    )
    line_widths = line_widths.clip(_COST_LINE_LOWER, _COST_LINE_UPPER).replace(
        _COST_LINE_LOWER, 0.0
    )
    link_widths = link_widths.clip(_COST_LINE_LOWER, _COST_LINE_UPPER).replace(
        _COST_LINE_LOWER, 0.0
    )

    colors = _map_colors()
    tech_names = costs.index.get_level_values(1).unique()
    bus_colors = pd.Series(
        {t: resolve_tech_color(str(t), colors) for t in tech_names}
    )
    proj = ccrs.EqualEarth()
    fig, ax = plt.subplots(figsize=_MAP_FIGSIZE, subplot_kw={"projection": proj})
    ax.set_extent(_MAP_BOUNDARIES, crs=ccrs.PlateCarree())

    plot_regions = regions.copy()
    plot_regions["cost_pct_gdp"] = plot_regions.index.map(ratio)
    plot_regions.plot(
        ax=ax,
        column="cost_pct_gdp",
        cmap="Greys",
        linewidths=0,
        legend=False,
        vmin=0,
        vmax=6,
        zorder=1,
    )

    n.plot(
        ax=ax,
        bus_size=costs / _COST_BUS_SIZE_FACTOR,
        bus_color=bus_colors,
        line_color=_AC_COLOR,
        link_color=_DC_COLOR,
        line_width=line_widths / _COST_LINEWIDTH_FACTOR,
        link_width=link_widths / _COST_LINEWIDTH_FACTOR,
        **_map_opts(),
    )

    _outline_highlight(ax, regions, highlight)

    # Size legends
    sizes = [20, 10, 5]
    labels = [f"{s} bEUR/year" for s in sizes]
    legend_sizes = [s / _COST_BUS_SIZE_FACTOR * 1e9 for s in sizes]
    add_legend_circles(
        ax,
        legend_sizes,
        labels,
        srid=n.srid,
        patch_kw={"facecolor": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0.001, 0.98),
            "labelspacing": 1,
            "frameon": False,
            "handletextpad": 1,
            "fontsize": 15,
            "title": "Annualised Investment Costs",
        },
    )
    line_sizes = [10, 5, 1]
    line_labels = [f"{s} GW" for s in line_sizes]
    scale = 1e3 / _COST_LINEWIDTH_FACTOR
    add_legend_lines(
        ax,
        [s * scale for s in line_sizes],
        line_labels,
        patch_kw={"color": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0.45, 0.98),
            "frameon": False,
            "labelspacing": 1,
            "handletextpad": 1,
            "fontsize": 15,
            "title": "total grid capacity",
        },
    )

    sm = plt.cm.ScalarMappable(cmap="Greys", norm=plt.Normalize(vmin=0, vmax=6))
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, orientation="vertical", shrink=0.5, pad=0.02)
    cbar.set_label(r"Cost [% GDP$_{2023}$ / year]", fontsize=15)
    cbar.ax.tick_params(labelsize=15)

    legend_colors = [resolve_tech_color(c, colors) for c in carrier_list] + [
        _AC_COLOR,
        _DC_COLOR,
    ]
    legend_labels = carrier_list + ["AC line", "DC line"]
    handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor=color,
            markersize=15,
        )
        for color in legend_colors
    ]
    fig.legend(
        handles,
        legend_labels,
        bbox_to_anchor=(0.9, 0.25),
        ncol=4,
        frameon=False,
        fontsize=15,
    )
    fig.tight_layout()
    return fig


def _render_hydrogen_map(
    network, regions: gpd.GeoDataFrame, ctx, *, highlight: str | None = None
) -> Figure | None:
    if network.links.empty:
        logger.warning("hydrogen map: network has no links")
        return None

    h2_links = network.links.carrier.str.contains("H2 pipeline", na=False)
    if not h2_links.any():
        logger.warning("hydrogen map: no H2 pipeline links in network")
        return None

    from .maps_pipes import group_pipes

    n = network.copy()
    _assign_locations(n)

    bus_size_factor = 1e5
    linewidth_factor = 7e3
    line_lower_threshold = 750.0

    # Storage choropleth (TWh)
    plot_regions = regions.copy()
    if not n.stores.empty:
        h2_store = n.stores.loc[n.stores.carrier == "H2"]
        if not h2_store.empty:
            bus_loc = (
                h2_store["location"]
                if "location" in h2_store.columns
                else h2_store.bus.map(n.buses.location)
            )
            storage_twh = h2_store["e_nom_opt"].groupby(bus_loc).sum() / 1e6
            storage_twh = storage_twh.where(storage_twh > 0.1)
            plot_regions["H2_TWh"] = plot_regions.index.map(storage_twh)

    _drop_non_ac_buses(n)

    # Electrolysis bus sizes
    elec = n.links.index[n.links.carrier == "H2 Electrolysis"]
    bus_sizes = pd.Series(dtype=float)
    if len(elec):
        bus_sizes = (
            n.links.loc[elec, "p_nom_opt"]
            .groupby([n.links.loc[elec, "bus0"], n.links.loc[elec, "carrier"]])
            .sum()
            / bus_size_factor
        )
        bus_sizes = bus_sizes.rename(
            index=lambda x: str(x).replace(" H2", ""), level=0
        )

    # Keep only H2 pipelines
    n.links.drop(n.links.index[~n.links.carrier.str.contains("H2 pipeline")], inplace=True)

    h2_new = n.links[n.links.carrier == "H2 pipeline"]
    h2_retro = n.links[n.links.carrier == "H2 pipeline retrofitted"]

    if not h2_new.empty:
        # Group parallel / multi-period pipes when present.
        if h2_new.index.astype(str).str.contains(r"-2|-3|-4").any() or (
            h2_new.groupby(["bus0", "bus1"]).size() > 1
        ).any():
            h2_new = group_pipes(h2_new)

    if not h2_retro.empty:
        positive_order = h2_retro.bus0 < h2_retro.bus1
        h2_retro_p = h2_retro[positive_order]
        h2_retro_n = h2_retro[~positive_order].rename(
            columns={"bus0": "bus1", "bus1": "bus0"}
        )
        h2_retro = pd.concat([h2_retro_p, h2_retro_n])
        h2_retro["index_orig"] = h2_retro.index
        h2_retro.index = h2_retro.apply(
            lambda x: (
                f"H2 pipeline {str(x.bus0).replace(' H2', '')}"
                f" -> {str(x.bus1).replace(' H2', '')}"
            ),
            axis=1,
        )
        if not h2_new.empty:
            retro_w_new = h2_retro.index.intersection(h2_new.index)
            retro_wo_new = h2_retro.index.difference(h2_new.index)
            parts = [h2_new]
            if len(retro_w_new):
                parts.append(h2_retro.loc[retro_w_new])
            if len(retro_wo_new):
                wo = h2_retro.loc[retro_wo_new].copy()
                wo.index = wo["index_orig"]
                parts.append(wo)
            h2_total = pd.concat(parts).p_nom_opt.groupby(level=0).sum()
        else:
            h2_total = h2_retro.p_nom_opt.groupby(level=0).sum()
    else:
        h2_total = h2_new.p_nom_opt if not h2_new.empty else pd.Series(dtype=float)

    # Collapse duplicate link names after grouping.
    n.links.rename(index=lambda x: str(x).split("-2")[0], inplace=True)
    n.links = n.links.groupby(level=0).first()
    link_widths_total = h2_total.reindex(n.links.index).fillna(0.0) / linewidth_factor
    link_widths_total[n.links.p_nom_opt < line_lower_threshold] = 0.0

    retro = n.links.p_nom_opt.where(
        n.links.carrier == "H2 pipeline retrofitted", other=0.0
    )
    link_widths_retro = retro / linewidth_factor
    link_widths_retro[n.links.p_nom_opt < line_lower_threshold] = 0.0

    n.links.bus0 = n.links.bus0.str.replace(" H2", "", regex=False)
    n.links.bus1 = n.links.bus1.str.replace(" H2", "", regex=False)

    color_h2_pipe = "#c5c9c7"
    color_retrofit = "#11875d"
    bus_colors = {"H2 Electrolysis": "#ffbacd"}

    proj = ccrs.EqualEarth()
    fig, ax = plt.subplots(figsize=_MAP_FIGSIZE, subplot_kw={"projection": proj})

    if "H2_TWh" in plot_regions.columns:
        plot_regions.plot(
            ax=ax,
            column="H2_TWh",
            cmap="Blues",
            linewidths=0,
            legend=True,
            vmax=6,
            vmin=0,
            legend_kwds={
                "label": "Hydrogen Storage [TWh]",
                "shrink": 0.7,
                "extend": "max",
            },
            zorder=1,
        )
    else:
        regions.plot(ax=ax, facecolor="none", edgecolor="lightgrey", linewidth=0.3)

    n.plot(
        ax=ax,
        bus_size=bus_sizes if len(bus_sizes) else 0.0,
        bus_color=bus_colors,
        link_color=color_h2_pipe,
        link_width=link_widths_total,
        branch_components=["Link"],
        **_map_opts(),
    )
    n.plot(
        ax=ax,
        bus_size=0.0,
        link_color=color_retrofit,
        link_width=link_widths_retro,
        branch_components=["Link"],
        **_map_opts(),
    )

    _outline_highlight(ax, regions, highlight)

    sizes = [50, 10]
    labels = [f"{s} GW" for s in sizes]
    add_legend_circles(
        ax,
        [s / bus_size_factor * 1e3 for s in sizes],
        labels,
        srid=n.srid,
        patch_kw={"facecolor": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0.05, 1),
            "labelspacing": 1.2,
            "frameon": False,
            "fontsize": 15,
            "title": "Electrolyser capacity",
        },
    )
    line_sizes = [30, 10]
    scale = 1e3 / linewidth_factor
    add_legend_lines(
        ax,
        [s * scale for s in line_sizes],
        [f"{s} GW" for s in line_sizes],
        patch_kw={"color": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0.05, 0.8),
            "frameon": False,
            "fontsize": 15,
            "title": "H2 pipeline capacity",
        },
    )
    add_legend_patches(
        ax,
        ["#ffbacd", color_h2_pipe, color_retrofit],
        ["H2 Electrolysis", "H2 pipeline (total)", "H2 pipeline (repurposed)"],
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0.05, 0.6),
            "frameon": False,
            "fontsize": 15,
        },
    )
    fig.tight_layout()
    return fig


def _render_gas_map(
    network, regions: gpd.GeoDataFrame, ctx, *, highlight: str | None = None
) -> Figure | None:
    if network.links.empty:
        logger.warning("gas map: network has no links")
        return None

    gas_links = network.links.carrier.str.contains("gas pipeline", na=False)
    if not gas_links.any():
        logger.warning("gas map: no gas pipeline links in network")
        return None

    n = network.copy()
    _assign_locations(n)

    bus_size_factor = 10e8
    linewidth_factor = 0.5e4
    line_lower_threshold = 1e3
    weights = n.snapshot_weightings.generators

    def _gen_supply(carrier: str, label: str) -> pd.Series:
        idx = n.generators.index[n.generators.carrier == carrier]
        if not len(idx) or n.generators_t.p.empty:
            return pd.Series(dtype=float)
        series = (
            n.generators_t.p.loc[:, idx]
            .mul(weights, axis=0)
            .sum()
            .groupby(n.generators.loc[idx, "bus"])
            .sum()
            / bus_size_factor
        )
        series = series.rename(
            index=lambda x: str(x).replace(f" {carrier}", "").replace(" gas", "")
        )
        series.index = pd.MultiIndex.from_product([series.index, [label]])
        return series

    # Supply pies must be computed before dropping non-AC buses.
    fossil_gas = _gen_supply("gas", "fossil gas")
    biogas = _gen_supply("biogas", "biogas")

    methanation = pd.Series(dtype=float)
    sab = n.links.query("carrier == 'Sabatier'").index
    if len(sab) and not n.links_t.p1.empty:
        methanation = (
            abs(n.links_t.p1.loc[:, sab].mul(weights, axis=0))
            .sum()
            .groupby(n.links.loc[sab, "bus1"])
            .sum()
            / bus_size_factor
        )
        methanation = methanation.rename(index=lambda x: str(x).replace(" gas", ""))
        methanation.index = pd.MultiIndex.from_product(
            [methanation.index, ["methanation"]]
        )

    bus_sizes = pd.concat([s for s in (fossil_gas, methanation, biogas) if len(s)])
    if not bus_sizes.empty:
        bus_sizes = bus_sizes.sort_index()

    _drop_non_ac_buses(n)
    if not bus_sizes.empty:
        keep = bus_sizes.index.get_level_values(0).isin(n.buses.index)
        bus_sizes = bus_sizes.loc[keep]

    n.links.drop(
        n.links.index[~n.links.carrier.str.contains("gas pipeline")], inplace=True
    )

    link_widths_rem = n.links.p_nom_opt / linewidth_factor
    link_widths_rem[n.links.p_nom_opt < line_lower_threshold] = 0.0
    link_widths_orig = n.links.p_nom / linewidth_factor
    link_widths_orig[n.links.p_nom < line_lower_threshold] = 0.0

    if not n.links_t.p0.empty:
        max_usage = n.links_t.p0[n.links.index].abs().max(axis=0)
    else:
        max_usage = n.links.p_nom_opt * 0.0
    link_widths_used = max_usage / linewidth_factor
    link_widths_used[max_usage < line_lower_threshold] = 0.0

    pipe_colors = {
        "gas pipeline": "#ffb07c",
        "gas pipeline new": "#c14a09",
        "gas pipeline retrofitted to H2": "#11875d",
        "gas pipeline (available)": "#fbeeac",
    }
    link_color_used = n.links.carrier.map(pipe_colors).fillna("#c14a09")

    n.links.bus0 = n.links.bus0.str.replace(" gas", "", regex=False)
    n.links.bus1 = n.links.bus1.str.replace(" gas", "", regex=False)

    # Optional EU gas node for pipes that terminate there (display only).
    if (n.links.bus0 == "EU gas").any() or (n.links.bus1 == "EU gas").any():
        if "EU gas" not in n.buses.index:
            n.buses.loc["EU gas", "x"] = -5.5
            n.buses.loc["EU gas", "y"] = 46.0
            n.buses.loc["EU gas", "carrier"] = "AC"
        else:
            n.buses.loc["EU gas", "x"] = -5.5
            n.buses.loc["EU gas", "y"] = 46.0

    bus_colors = pd.Series(
        {
            "fossil gas": "#9a0200",
            "methanation": "#ffbacd",
            "biogas": "#32bf84",
        }
    )

    proj = ccrs.EqualEarth()
    fig, ax = plt.subplots(figsize=_MAP_FIGSIZE, subplot_kw={"projection": proj})
    ax.set_extent(_MAP_BOUNDARIES, crs=ccrs.PlateCarree())
    regions.plot(
        ax=ax, facecolor="none", edgecolor="lightgrey", linewidth=0.3, zorder=1
    )

    plot_kwargs = {
        "ax": ax,
        "branch_components": ["Link"],
        **_map_opts(),
    }
    n.plot(
        bus_size=bus_sizes if len(bus_sizes) else 0.0,
        bus_color=bus_colors,
        link_color=pipe_colors["gas pipeline retrofitted to H2"],
        link_width=link_widths_orig,
        **plot_kwargs,
    )
    n.plot(
        bus_size=0.0,
        link_color=pipe_colors["gas pipeline (available)"],
        link_width=link_widths_rem,
        **plot_kwargs,
    )
    n.plot(
        bus_size=0.0,
        link_color=link_color_used,
        link_width=link_widths_used,
        **plot_kwargs,
    )

    _outline_highlight(ax, regions, highlight)

    sizes = [100, 10]
    labels = [f"{s} TWh" for s in sizes]
    add_legend_circles(
        ax,
        [s / bus_size_factor * 1e6 for s in sizes],
        labels,
        srid=n.srid,
        patch_kw={"facecolor": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0, 0.8),
            "labelspacing": 0.8,
            "frameon": False,
            "handletextpad": 1,
            "fontsize": 15,
            "title": "gas sources supply",
        },
    )
    line_sizes = [50, 10]
    scale = 1e3 / linewidth_factor
    add_legend_lines(
        ax,
        [s * scale for s in line_sizes],
        [f"{s} GW" for s in line_sizes],
        patch_kw={"color": "black"},
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0, 0.6),
            "frameon": False,
            "labelspacing": 0.8,
            "fontsize": 15,
            "handletextpad": 1,
            "title": "gas pipeline capacity",
        },
    )
    add_legend_patches(
        ax,
        [
            bus_colors["fossil gas"],
            bus_colors["methanation"],
            bus_colors["biogas"],
            pipe_colors["gas pipeline"],
            pipe_colors["gas pipeline new"],
            pipe_colors["gas pipeline (available)"],
        ],
        [
            "fossil gas",
            "methanation",
            "biogas",
            "gas pipeline",
            "gas pipeline new",
            "gas pipeline (available)",
        ],
        legend_kw={
            "loc": "upper left",
            "bbox_to_anchor": (0, 0.4),
            "frameon": False,
            "fontsize": 13,
        },
    )
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
