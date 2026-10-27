from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IconRecord:
    """One row of icons.json. See library-maintenance.md for what each field means."""
    id: str
    file: str
    style: str
    category: str
    tags: list[str]
    status: str = "approved"            # approved | sample | deprecated
    description: str = ""
    description_ar: str = ""
    tags_ar: list[str] = field(default_factory=list)
    preferred_for: list[str] = field(default_factory=list)  # concepts where this is the house pick
    avoid_for: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    metaphor_strength: str = "strong"   # strong | weak
    pairs_with: list[str] = field(default_factory=list)
    rtl_flip: bool = False
    source: str = ""
    license: str = ""


@dataclass
class Item:
    """One thing that may get an icon: a bullet, card, column, step or KPI tile."""
    slide: int
    shape_id: int
    text: str
    kind: str                   # list | card | column | hero
    left: float                 # inches
    top: float
    w: float
    h: float
    font_pt: float = 16
    set_id: Optional[str] = None  # items decided together (a row of cards)
    rtl: bool = False
    paragraph: int = 0          # for list items inside one text box
    line: int = 0               # the paragraph's first line in its box, counting wrapped lines (estimated)
    concept: Optional[str] = None
    concepts: list[str] = field(default_factory=list)   # Claude's ideas for its icon, best first (concept = the one used)
    # Where the item's first line of text sits (inches, estimated), so an icon goes next to the text itself.
    # None: not measured (fall back to the box's edges).
    tx_left: Optional[float] = None
    tx_right: Optional[float] = None
    tx_top: Optional[float] = None
    line_h: Optional[float] = None
    align: str = "l"            # l | ctr | r
    in_shape: bool = False      # the text sits in a drawn shape (a node, tile or filled card)
    room: float = 0.0           # how far its text can move over (inches) without wrapping onto more lines
    rank: int = 0               # -1 labels of icon holders, 0 drawn components / heroes, 1 short labels,
                                # 2 longer text, 3 bullet lists
    slide_title: str = ""
    rows: list = field(default_factory=list)   # (left, top, right, bottom) of every line of its shape's text (estimated)
    holder: Optional[tuple] = None   # (center x, center y, largest icon size): the empty holder its icon goes in
    family: str = ""            # its set's structure: sets with the same family on different slides look alike


@dataclass
class DeckSettings:
    style: str
    color_hex: str              # the deck color's hex in this theme
    tiers: dict
    sources: dict               # where each setting came from, for the report
    color_slot: str = "accent1"  # the deck color: always a theme slot (accent1, tx1, bg1, ...)


@dataclass
class Pick:
    item: Item
    icon: IconRecord
    reason: str
    alternatives: list[str]
    left: float = 0
    top: float = 0
    size: float = 0
    flip: bool = False
    position: str = ""          # "above" or "beside" its text (one per set)
    indent: float = 0.0         # inches the text was moved over to make room for the icon (a layout change)
    color_slot: str = ""        # theme slot for this icon, chosen for contrast with what is behind it
    color_hex: str = ""
    color_note: str = ""        # why this color, for the report


@dataclass
class Flag:
    item: Item
    reason: str
    options: list[str] = field(default_factory=list)


@dataclass
class Report:
    mode: str
    settings: DeckSettings
    picks: list[Pick] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    output_file: Optional[str] = None
