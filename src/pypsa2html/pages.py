"""The page manifest: what the report contains and in which order.

The legacy tool decided this in four unsynchronised places -- a hardcoded
``sections`` list, a substring test on the section title to pick the output
file, a dict of pre-written ``<a href>`` strings, and prose keys in
``plots.yaml``.  Two pairs had already drifted apart in the shipped output, and
switching any plot off raised ``NameError`` because the page-assembly code
dereferenced a variable defined inside the disabled branch.

Here a single ``id`` per section serves as anchor, toggle key and narrative
key, and a disabled section is simply absent from the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MANIFEST_PATH = Path(__file__).parent / "data" / "pages.yaml"

#: Which nodes a section applies to.
SCOPES = frozenset({"all", "real", "aggregate"})


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    builder: str
    unit: str = ""
    enabled: bool = True
    scope: str = "all"

    def applies_to(self, node) -> bool:
        if self.scope == "all":
            return True
        if self.scope == "aggregate":
            return bool(getattr(node, "aggregate", False))
        return not getattr(node, "aggregate", False)


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    sections: tuple[Section, ...]
    kind: str = "single_scenario"
    #: True for pages whose content does not depend on the selected node.
    shared: bool = False

    def filename(self, node: str, scenario: str) -> str:
        """Output file name.  Kept compatible with the legacy convention."""
        if self.shared:
            return f"{self.id}_{scenario}.html"
        return f"{node}_{self.id}_{scenario}.html"


@dataclass
class Manifest:
    pages: list[Page] = field(default_factory=list)

    def __iter__(self):
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, page_id: str) -> Page:
        for p in self.pages:
            if p.id == page_id:
                return p
        raise KeyError(f"unknown page {page_id!r}; known: {[p.id for p in self.pages]}")

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self.pages]

    def section_ids(self) -> list[str]:
        return [s.id for p in self.pages for s in p.sections]


def load_manifest(
    *,
    path: str | Path | None = None,
    enable: dict[str, bool] | None = None,
    include_pages: list[str] | None = None,
) -> Manifest:
    """Load the manifest, apply per-section toggles and page selection.

    ``enable`` maps section ids to booleans and overrides the manifest default.
    A key that matches no section is an error -- in the legacy tool a misspelled
    ``plots.yaml`` key silently did nothing (and ``Cummulative``/``Comaprison``
    misspellings became load-bearing as a result).
    """
    with open(path or MANIFEST_PATH) as f:
        raw = yaml.safe_load(f) or {}

    enable = dict(enable or {})
    all_section_ids: set[str] = set()
    pages: list[Page] = []

    for page_raw in raw.get("pages", []):
        sections = []
        for s in page_raw.get("sections", []):
            sid = s["id"]
            if sid in all_section_ids:
                raise ValueError(f"duplicate section id {sid!r} in the page manifest")
            all_section_ids.add(sid)
            scope = s.get("scope", "all")
            if scope not in SCOPES:
                raise ValueError(
                    f"section {sid!r}: scope must be one of {sorted(SCOPES)}, got {scope!r}"
                )
            enabled = enable.get(sid, s.get("enabled", True))
            if not enabled:
                continue
            sections.append(
                Section(
                    id=sid,
                    title=s["title"],
                    builder=s["builder"],
                    unit=s.get("unit", ""),
                    enabled=True,
                    scope=scope,
                )
            )
        pages.append(
            Page(
                id=page_raw["id"],
                title=page_raw["title"],
                sections=tuple(sections),
                kind=page_raw.get("kind", "single_scenario"),
                shared=bool(page_raw.get("shared", False)),
            )
        )

    unknown = sorted(set(enable) - all_section_ids)
    if unknown:
        raise ValueError(
            f"the 'plots:' config refers to unknown section id(s) {unknown}. "
            f"Valid ids: {sorted(all_section_ids)}"
        )

    if include_pages is not None:
        known = {p.id for p in pages}
        missing = [p for p in include_pages if p not in known]
        if missing:
            raise ValueError(
                f"output.pages refers to unknown page(s) {missing}. Valid: {sorted(known)}"
            )
        order = {pid: i for i, pid in enumerate(include_pages)}
        pages = sorted((p for p in pages if p.id in order), key=lambda p: order[p.id])

    # A page whose sections were all switched off would render as an empty
    # shell with a dead nav entry.
    pages = [p for p in pages if p.sections]
    return Manifest(pages=pages)
