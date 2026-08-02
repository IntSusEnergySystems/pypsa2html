"""pypsa2html -- turn solved PyPSA(-Eur) networks into a navigable HTML report.

Typical use::

    from pypsa2html import load_config, build_site
    build_site(load_config("config/my-model.yaml"))

The package is a restructured, model-agnostic successor to the legacy reporting scripts
used in the négaWatt PyPSA-Eur studies.  See ``docs/DESIGN_DECISIONS.md`` for
what changed and why.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config  # noqa: E402
from .datafiles import Taxonomy, load_taxonomy  # noqa: E402
from .nodes import Node, NodeResolver, NodeSet  # noqa: E402
from .pages import Manifest, Page, Section, load_manifest  # noqa: E402

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "Taxonomy",
    "load_taxonomy",
    "Node",
    "NodeSet",
    "NodeResolver",
    "Manifest",
    "Page",
    "Section",
    "load_manifest",
    "build_site",
]


def build_site(config, **kwargs):
    """Build the full HTML report described by ``config``.

    Imported lazily so that ``import pypsa2html`` stays cheap and does not
    pull in pypsa/matplotlib/geopandas for callers that only need the config
    and taxonomy layers (notably the test suite).
    """
    from .build import build_site as _build_site

    return _build_site(config, **kwargs)
