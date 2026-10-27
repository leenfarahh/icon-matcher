"""Load the icon library: one flat icons.json next to the SVGs. The concept map is built from preferred_for."""
import json
import re
from pathlib import Path

from . import config
from .models import IconRecord

INDEX_FILE = "icons.json"


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.icons: dict[str, IconRecord] = {}   # approved only: what reaches client decks
        self.all: dict[str, IconRecord] = {}     # every record, any status (for editing and previews)
        self.concept_map: dict[str, dict] = {}
        self.style_guide: dict = {}
        self.indexed_from_filenames = False
        self.warnings: list[str] = []
        self._load()

    def _load(self):
        index = self.root / INDEX_FILE
        if index.exists():
            for rec in json.loads(index.read_text(encoding="utf-8")):
                icon = IconRecord(**{k: rec[k] for k in IconRecord.__dataclass_fields__ if k in rec})
                if icon.id in self.all:
                    self.warnings.append(f"duplicate id '{icon.id}': later row ignored")
                    continue
                self.all[icon.id] = icon
                if icon.status == "approved":   # only approved icons reach client decks
                    self.icons[icon.id] = icon
        if not self.all:
            self._index_from_filenames()
            self.all = dict(self.icons)
        self._build_concept_map()
        # TODO: parse style-guide.md into {style, color_hex, tiers}. Agree a small
        # front-matter block with the library owner so this is not free-text parsing.

    def _build_concept_map(self):
        """concept -> {"id": house pick, "alts": other icons tagged with the concept}. First claim wins."""
        for icon in self.icons.values():
            for concept in (c.lower() for c in icon.preferred_for):
                if concept in self.concept_map:
                    self.warnings.append(f"'{concept}' is preferred_for both "
                                         f"{self.concept_map[concept]['id']} and {icon.id}: first wins")
                    continue
                alts = [i.id for i in self.icons.values()
                        if i is not icon and concept in (t.lower() for t in i.tags)]
                self.concept_map[concept] = {"id": icon.id, "alts": alts}

    def _index_from_filenames(self):
        """No index: tags from file names. Lower confidence; the report says so."""
        self.indexed_from_filenames = True
        for svg in self.root.rglob("*.svg"):
            rel = svg.relative_to(self.root).as_posix()
            parts = svg.parent.relative_to(self.root).parts
            style = parts[1] if len(parts) > 1 else config.DEFAULT_STYLE
            self.icons[svg.stem] = IconRecord(
                id=svg.stem, file=rel, style=style, category=parts[-1] if parts else "general",
                tags=re.split(r"[-_ ]+", svg.stem.lower()))

    def get(self, icon_id: str) -> IconRecord | None:
        return self.icons.get(icon_id)

    def svg_bytes(self, icon: IconRecord) -> bytes:
        return (self.root / icon.file).read_bytes()

    def search(self, concept: str, style: str, arabic: bool = False, limit: int = 10) -> list[IconRecord]:
        """Score icons by tag and context overlap with the concept. Concept map is checked by the selector first.
        Arabic items match English and Arabic tags, since concepts usually come back in English."""
        words = set(re.split(r"[\s\-_/]+", concept.lower()))
        scored = []
        for icon in self.icons.values():
            if icon.style != style:
                continue
            tags = set(t.lower() for t in (icon.tags + icon.tags_ar if arabic else icon.tags))
            score = 3 * len(words & tags) + len(words & set(c.lower() for c in icon.contexts))
            if concept.lower() in tags:
                score += 5
            if score:
                scored.append((score, icon))
        scored.sort(key=lambda s: -s[0])
        return [i for _, i in scored[:limit]]
