"""Step 5a: where each icon goes. Positions in inches.

An icon sits right next to its text: beside the first line, or just above it, aligned the way the text is
(centered over centered text). It stays on the same surface as its text (inside the hexagon or card the
text is in, or on the slide when the text is), and never covers other content: text, pictures, lines,
tables, other cards or icons, including the layout's and master's decorations.

Each set (a component: all nodes of a diagram, all headings of a set of columns, a list) shares one
placement and gets icons on every member or none. Labels of empty icon holders get their icon inside the
holder. Text inside a drawn shape tries "above" (inside the shape) then "beside"; plain text tries "beside" then
"above"; lists go beside. When nothing fits, the whole set is flagged "no room". Sets with the same structure on
different slides (a family) share one placement and size."""
from collections import defaultdict

from lxml import etree

from . import colors, config
from .deck import _font_pt, text_lines
from .icon_tools import EMU
from .models import DeckSettings, Flag, Pick

TOUCH_IN = 0.03         # overlaps thinner than this are touching, not covering
MIN_THICK_IN = 0.08     # a hairline's hit area, so an icon can't sit on a divider line
BIG = 12                # a non-rectangular shape this many icon areas big blocks only where the icon's center is
TIGHT_GAP_IN = 0.04     # the smallest margin kept around an icon squeezed into a node above its label
NS = colors.NS


def gap_for(size: float) -> float:
    return max(0.08, 0.35 * size)


def _options(members) -> list[str]:
    it = members[0].item
    if all(p.item.holder for p in members):
        return ["holder"]
    if it.kind == "list":
        return ["beside"]
    if it.kind == "hero":
        return ["above"]
    # In a node or tile: above the label, or centered in a tight space above it; then beside. Plain text: beside.
    # Last resort (style guide, Rows): move the text over by the icon and gap, and put the icon where it began.
    first = ["above", "tight", "beside"] if all(p.item.in_shape for p in members) else ["beside", "above"]
    return first + ["indent"]


def priority(items) -> tuple:
    """Order in which sets claim a slide (and icons): slide, then rank, larger text, larger shapes."""
    it = items[0]
    return it.slide, it.rank, -it.font_pt, -sum(i.w * i.h for i in items) / len(items)


def one_set_reason(taken: str) -> str:
    return f"one icon set per slide: this slide already gets icons on '{taken}' and the items in its set"


def main_only_reason(main: str) -> str:
    return (f"only the slide's main component gets icons ('{main}' and the items in its set), and it couldn't "
            "take them; see its reason")


class Placer:
    """Places sets one at a time on a deck, remembering what each slide already has."""

    def __init__(self, settings: DeckSettings, prs):
        self.settings, self.prs = settings, prs
        self.slide_w, self.slide_h = prs.slide_width / EMU, prs.slide_height / EMU
        self.content, self.placed, self.done = {}, defaultdict(list), {}

    def taken(self, slide: int) -> str | None:
        """With one icon set per slide: the set that already has this slide's icons."""
        return self.done.get(slide) if config.ONE_ICON_SET_PER_SLIDE else None

    def _try(self, members: list[Pick], how: str, size: float):
        """(spots, failures) for one option at one size, without placing anything."""
        n = members[0].item.slide
        if n not in self.content:
            self.content[n] = _content(self.prs.slides[n - 1])
        spots = [_spot(p.item, how, size) for p in members]
        if how == "indent":
            fails = [(p.item, why) for p in members if (why := _cant_indent(p.item, size))]
            if fails:
                return spots, fails
        if how == "holder":
            return spots, [(p.item, why) for p, spot in zip(members, spots)
                           if (why := _holder_blocked(spot, size, self.content[n], self.placed[n]))]
        shift = size + gap_for(size) if how == "indent" else 0.0
        return spots, [(p.item, why) for p, spot in zip(members, spots)
                       if (why := _blocked(p.item, spot, size, spots, self.content[n], self.placed[n],
                                           self.slide_w, self.slide_h, shift))]

    def place_family(self, sets: list[list[Pick]]) -> list[list[Flag]]:
        """Place sets that share a structure on different slides with one option and size, all or none (a
        family of one is just a set). Falls back to each set's own option when no shared one fits them all.
        Returns each set's flags; all empty when placed."""
        plans, closest = None, [None] * len(sets)
        for how in _options(sets[0]):
            size = min(_size(m, how, self.settings.tiers) for m in sets)
            tries = [self._try(m, how, size) for m in sets]
            for k, (_, fails) in enumerate(tries):   # on a tie the later option says more, but "can't indent" least
                if fails and (closest[k] is None or (how != "indent" and len(fails) <= len(closest[k]))):
                    closest[k] = fails
            if not any(fails for _, fails in tries):
                plans = [(how, size, spots) for spots, _ in tries]
                break
        if plans is None and len(sets) > 1:   # no shared placement: each set's own, still all or none
            own = []
            for m in sets:
                for how in _options(m):
                    size = _size(m, how, self.settings.tiers)
                    spots, fails = self._try(m, how, size)
                    if not fails:
                        own.append((how, size, spots))
                        break
                else:
                    own.append(None)
            plans = own if all(own) else None
        if plans is None:
            return [self._no_room(m, closest[k]) if closest[k] else [] for k, m in enumerate(sets)]
        for m, (how, size, spots) in zip(sets, plans):
            self._commit(m, how, size, spots)
        return [[] for _ in sets]

    @staticmethod
    def _no_room(members, closest) -> list[Flag]:
        culprit, problem = closest[0]
        where = "" if len(members) == 1 else (
            f" on '{' '.join(culprit.text.split())[:40]}'" + (f" and {len(closest) - 1} more" if len(closest) > 1
                                                              else "") + " (its set gets icons on all or none)")
        return [Flag(p.item, f"no room for the icon{where}: {problem}", [p.icon.id]) for p in members]

    def place_set(self, members: list[Pick]) -> list[Flag]:
        """Position every member, or flag them all. Returns the flags (empty when placed)."""
        return self.place_family([members])[0]

    def _commit(self, members, how, size, spots):
        n = members[0].item.slide
        for p, (left, top) in zip(members, spots):
            p.left, p.top, p.size = left, top, size
            p.position = {"tight": "above", "indent": "beside", "holder": "in its holder"}.get(how, how)
            p.indent = round(size + gap_for(size), 3) if how == "indent" else 0.0
            p.flip = p.item.rtl and p.icon.rtl_flip
            self.placed[n].append((left, top, left + size, top + size))
        self.done[n] = " ".join(members[0].item.text.split())[:40]


def place(picks: list[Pick], settings: DeckSettings, prs) -> list[Flag]:
    """Place picked icons set by set, most prominent first."""
    sets = defaultdict(list)
    for p in picks:
        sets[p.item.set_id or f"solo-{id(p)}"].append(p)
    placer, flags = Placer(settings, prs), []
    for members in sorted(sets.values(), key=lambda m: priority([p.item for p in m])):
        taken = placer.taken(members[0].item.slide)
        flags += [Flag(p.item, one_set_reason(taken), [p.icon.id]) for p in members] if taken \
            else placer.place_set(members)
    return flags


def _cant_indent(it, size: float) -> str | None:
    """Why the text can't move over to make room, or None. Only text that starts at its reading-start edge
    (left, or right in Arabic) and still fits on the same number of lines once moved."""
    if it.kind not in ("card", "hero") or it.tx_left is None:
        return "only a heading or label can move over to make room"
    if it.align != ("r" if it.rtl else "l"):
        return "its text is centered, so moving it over wouldn't make room at its start"
    if it.room < size + gap_for(size):
        return "its text would wrap onto another line if moved over to make room"
    return None


def _size(members, how: str, tiers: dict) -> float:
    """One size for the set. Beside text, the tier closest to the first line's height (style guide: an icon by
    a line of text is roughly that line's height); above it, the set's own tier (card, hero); in holders, what
    the smallest holder takes."""
    kind = members[0].item.kind
    if how == "holder":
        return min(p.item.holder[2] for p in members)
    if how not in ("beside", "indent"):
        return tiers[kind]
    heights = sorted(_text(p.item)[3] for p in members)
    line = heights[len(heights) // 2]
    return min((tiers["list"], tiers["card"]), key=lambda s: abs(s - line))


def _text(it):
    """(left, right, top, line height, alignment) of the item's first line; box edges when not measured."""
    inset = config.TEXT_INSET_IN
    line_h = it.line_h or it.font_pt * 1.2 / 72
    if it.tx_left is None:
        return it.left + inset, it.left + it.w - inset, it.top + inset / 2 + it.line * line_h, line_h, "l"
    return it.tx_left, it.tx_right, it.tx_top, line_h, it.align


def _spot(it, how: str, size: float) -> tuple[float, float]:
    """(left, top) of the icon: beside the first line on the reading-start side, or just above it."""
    if how == "holder":
        return it.holder[0] - size / 2, it.holder[1] - size / 2
    left, right, top, line_h, align = _text(it)
    gap = gap_for(size)
    if how == "beside":   # clear of every line of its text the icon sits level with (a wider second line)
        y = top + line_h / 2 - size / 2
        level = [r for r in it.rows if r[1] < y + size - TOUCH_IN and r[3] > y + TOUCH_IN]
        x = (max([right] + [r[2] for r in level]) + gap) if it.rtl else (min([left] + [r[0] for r in level]) - gap - size)
        return x, y
    if how == "indent":   # where the text begins now; the text moves over by size + gap
        return (right - size) if it.rtl else left, top + line_h / 2 - size / 2
    if align == "ctr":
        x = (left + right - size) / 2
    elif it.rtl or align == "r":
        x = right - size
    else:
        x = left
    if how == "tight":   # centered in the space between the node's top and its label
        return x, it.top + (top - it.top - size) / 2
    return x, top - gap - size


def _blocked(it, spot, size, set_spots, content, placed, slide_w, slide_h, shift=0.0) -> str | None:
    """Why the icon can't go here, or None when the spot is free. `shift`: inches the item's text moves over
    to make room (the icon then sits where the text began)."""
    l, t = spot
    icon = (l, t, l + size, t + size)
    m = config.SAFE_MARGIN_IN
    if l < m or t < m or l + size > slide_w - m or t + size > slide_h - m:
        return "it would sit too close to the slide edge"
    left, right, top, line_h, _ = _text(it)
    now_left, now_right = (left, right - shift) if it.rtl else (left + shift, right)
    if top - (t + size) < TIGHT_GAP_IN and t < top and l < now_right and l + size > now_left:
        return f"it would touch its own text"
    moved = [(a + (0 if it.rtl else shift), b, c - (shift if it.rtl else 0), d) for a, b, c, d in it.rows]
    if any(_overlap(icon, r) for r in moved):
        return "it would cover its own text"
    surface = _surface(content, ((right - 0.02) if it.rtl else (left + 0.02), top + line_h / 2))
    if surface is not None and not _fits(icon, surface):
        return "it would stick out of the shape its text is on"
    if surface is not None and surface[1] and _overlap(icon, surface[1]) and surface[3].shape_id != it.shape_id:
        return f"it would cover the text '{_short(surface[3])}'"   # the card it sits on carries a heading
    if surface is None and _surface(content, ((l + size / 2), (t + size / 2))) is not None:
        return "it would sit on a different shape than its text"
    own = (it.left, it.top, it.left + it.w, it.top + it.h)
    # On its text's surface, only what is layered above that surface can be covered; what lies beneath is hidden.
    beneath = next((k for k, e in enumerate(content) if e is surface), -1)
    for entry in content[beneath + 1:] if surface is not None else content:
        box, text_area, drawn, sh, on_slide, rectish, filled, geom, rot = entry
        if (on_slide and sh.shape_id == it.shape_id) or not _overlap(icon, box):
            continue
        if _inside(icon, box) and _inside(own, box):   # its own card or the backdrop: fine, but not its text
            if text_area and _overlap(icon, text_area):
                return f"it would cover the text '{_short(sh)}'"
        elif drawn and (rectish or (box[2] - box[0]) * (box[3] - box[1]) < BIG * size * size
                        or _touches(icon, entry)):
            return f"it would cover {_describe(sh)}"
        elif text_area and _overlap(icon, text_area):
            return f"it would cover the text '{_short(sh)}'"
    if any(_overlap(icon, other) for other in placed):
        return "it would cover another icon"
    if sum(_overlap(icon, (x, y, x + size, y + size)) for x, y in set_spots) > 1:
        return "the icons in this set would overlap each other"
    return None


def _holder_blocked(spot, size, content, placed) -> str | None:
    """In a holder, the icon may cover only the holder's own shapes and what lies under it: anything it
    would partly cover, or any text, blocks it."""
    l, t = spot
    icon = (l, t, l + size, t + size)
    for box, text_area, drawn, sh, *_ in content:
        if text_area and _overlap(icon, text_area):
            return f"it would cover the text '{_short(sh)}'"
        if drawn and _overlap(icon, box) and not _inside(icon, box):
            return f"it would cover {_describe(sh)}"
    if any(_overlap(icon, other) for other in placed):
        return "it would cover another icon"
    return None


def _corners(icon, inset=TOUCH_IN):
    l, t, r, b = icon
    return [(l + inset, t + inset), (r - inset, t + inset), (r - inset, b - inset), (l + inset, b - inset)]


def _fits(icon, entry) -> bool:
    """The icon lies within the shape's actual outline (an ellipse or hexagon, not just its box), keeping a
    TIGHT_GAP_IN margin from its edge."""
    (l, t, r, b), m = entry[0], TIGHT_GAP_IN
    if icon[0] - m < l or icon[1] - m < t or icon[2] + m > r or icon[3] + m > b:
        return False
    return all(colors.inside_outline(entry[7], entry[8], entry[0], x, y) for x, y in _corners(icon, -m))


def _touches(icon, entry) -> bool:
    """The icon's center or a corner falls inside a non-rectangular shape's actual outline."""
    center = ((icon[0] + icon[2]) / 2, (icon[1] + icon[3]) / 2)
    return any(colors.inside_outline(entry[7], entry[8], entry[0], x, y) for x, y in _corners(icon) + [center])


def _surface(content, point):
    """The topmost filled shape or picture whose outline holds a point (what the text or icon sits on);
    None = the slide."""
    found = None
    for entry in content:
        if entry[6] and colors.inside_outline(entry[7], entry[8], entry[0], *point):
            found = entry
    return found


def _content(slide) -> list:
    """(box, text area or None, drawn?, shape, on the slide itself?, rectangular?, filled?, outline, rotation)
    for every visible shape, in inches and z-order, including the layout's and master's decorations.
    Hairlines get a minimum thickness so they can be hit."""
    theme = colors.Theme(slide.slide_layout.slide_master)
    out = []
    for shapes, on_slide in colors.layers(slide):
        for (l, t, r, b), fill, sh in colors.boxes(theme, shapes):
            l, t, r, b = l / EMU, t / EMU, r / EMU, b / EMU
            if r - l < MIN_THICK_IN:
                l, r = (l + r - MIN_THICK_IN) / 2, (l + r + MIN_THICK_IN) / 2
            if b - t < MIN_THICK_IN:
                t, b = (t + b - MIN_THICK_IN) / 2, (t + b + MIN_THICK_IN) / 2
            text = sh.has_text_frame and sh.text_frame.text.strip()
            drawn = colors.is_drawn(sh, fill)
            if text or drawn:
                geom = colors.geometry(sh)
                rectish = geom in colors.RECTISH or geom in ("picture", "table")
                out.append(((l, t, r, b), _text_area(sh, (l, t, r, b)) if text else None, drawn, sh, on_slide,
                            rectish, fill is not None, geom, getattr(sh, "rotation", 0) or 0))
    return out


def _text_area(sh, box):
    """Where the text sits in its box: full width, estimated height, at the box's vertical anchor."""
    l, t, r, b = box
    font = _font_pt(sh)
    lines = sum(text_lines(p.text.strip(), r - l, font) for p in sh.text_frame.paragraphs if p.text.strip())
    height = min(b - t, lines * font * 1.2 / 72 + 0.1)
    body = sh._element.find(".//a:bodyPr", NS)
    cnv = sh._element.find("p:nvSpPr/p:cNvSpPr", NS)
    is_text_box = cnv is not None and cnv.get("txBox") == "1"
    anchor = (body.get("anchor") if body is not None else None) or ("t" if is_text_box else "ctr")
    top = t if anchor == "t" else (b - height if anchor == "b" else (t + b - height) / 2)
    return l, top, r, top + height


def _overlap(a, b) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > TOUCH_IN and min(a[3], b[3]) - max(a[1], b[1]) > TOUCH_IN


def _inside(a, b) -> bool:
    return a[0] >= b[0] - TOUCH_IN and a[1] >= b[1] - TOUCH_IN and a[2] <= b[2] + TOUCH_IN and a[3] <= b[3] + TOUCH_IN


def _contains(box, point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _short(sh) -> str:
    text = " ".join(sh.text_frame.text.split())
    return text[:40] + ("…" if len(text) > 40 else "")


def _describe(sh) -> str:
    kind = etree.QName(sh._element).localname
    if sh.has_text_frame and sh.text_frame.text.strip():
        return f"the text '{_short(sh)}'"
    return {"pic": "a picture", "cxnSp": "a line", "graphicFrame": "a table or chart"}.get(kind, "a shape")
