"""Icon colors, always from the deck's theme, chosen per icon for contrast with what sits behind it.

Every color is a theme slot (accent1, tx1, bg1, ...), resolved to hex through the slide master's color map
and theme, so the inserted SVG can be bound to the slot. What sits behind an icon is read at nine points over
its box: the slide background with every filled shape and picture under each point composited on top in
z-order, so a 15%-opacity card counts as the pale tint it shows (groups included), a gradient counts as the
color it has there (a dark badge with a light rim is dark in the middle) and a picture as its pixels there.
Then, per icon set (a row of cards shares one color), the first that reads on all of it:
  1. the placeholder's color, when the designer colored the PL logo placeholders;
  2. the deck color, if it has at least 3:1 contrast (the WCAG minimum for graphics);
  3. else the label's own text color, if it reaches 2:1 (the designer already uses it there for text);
  4. else the theme's dark or light colors at 3:1; failing all, the best of them, flagged in the note.
Reading also means going the right way: a light icon where the label is light (or, label unknown, where white
stands out more than black), a dark one where it is dark, so black never lands on a dark header just because
it reaches 3:1."""
import colorsys
import math
import re
import weakref
from collections import Counter, defaultdict

from lxml import etree
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .icon_tools import EMU, THEME_CLASS

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
MIN_CONTRAST = 3.0
LABEL_MIN_CONTRAST = 2.0
FALLBACK_SLOTS = ["tx1", "bg1", "tx2", "bg2"]
DEFAULT_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}
PRESET = {"black": "000000", "white": "FFFFFF"}
IMAGE = "image"         # a picture or other fill whose color can't be read
_UNSET = object()       # no fill element at all (fall back to the shape style)


def _name(el) -> str:
    return etree.QName(el).localname


class Theme:
    """A master's theme colors and color map."""

    def __init__(self, master):
        self.scheme = {}
        theme = next((r.target_part for r in master.part.rels.values() if r.reltype.endswith("/theme")), None)
        if theme is not None:
            scheme = etree.fromstring(theme.blob).find(".//a:clrScheme", NS)
            for slot in scheme if scheme is not None else []:
                if len(slot):
                    c = slot[0]
                    self.scheme[_name(slot)] = (c.get("val") if _name(c) == "srgbClr" else c.get("lastClr", "000000")).upper()
        cmap = master._element.find("p:clrMap", NS)
        self.map = dict(cmap.attrib) if cmap is not None else dict(DEFAULT_MAP)

    def slot_hex(self, slot: str) -> str:
        return self.scheme.get(self.map.get(slot, slot), "000000")

    def slot_for_hex(self, hexv: str) -> str | None:
        """The theme slot with exactly this color, if any (black text is tx1 in most themes)."""
        return next((s for s in FALLBACK_SLOTS + [f"accent{n}" for n in range(1, 7)] if self.slot_hex(s) == hexv), None)

    def color(self, el) -> tuple[str, str | None, float]:
        """(hex, theme slot or None, opacity) for a DrawingML color element, lumMod/lumOff/tint/shade applied.
        The slot is None when the color was lightened or darkened, or matches no theme color."""
        kind, slot = _name(el), None
        if kind == "schemeClr":
            slot = el.get("val")
            if slot in ("dk1", "lt1", "dk2", "lt2"):  # rare direct use: name it the way slides do
                slot = {v: k for k, v in self.map.items()}.get(slot, slot)
            hexv = self.slot_hex(slot)
        elif kind == "srgbClr":
            hexv = el.get("val", "000000").upper()
        elif kind == "sysClr":
            hexv = el.get("lastClr", "000000").upper()
        elif kind == "prstClr":
            hexv = PRESET.get(el.get("val"), "000000")
        else:
            return "000000", None, 1.0
        alpha = next((int(t.get("val", "100000")) / 100000 for t in el if _name(t) == "alpha"), 1.0)
        if any(_name(t) in ("lumMod", "lumOff", "tint", "shade") for t in el):
            return _transform(hexv, el), None, alpha
        return hexv, slot or self.slot_for_hex(hexv), alpha


def _transform(hexv: str, el) -> str:
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    for t in el:
        kind, v = _name(t), int(t.get("val", "100000")) / 100000
        if kind == "lumMod":
            l *= v
        elif kind == "lumOff":
            l += v
        elif kind in ("tint", "shade"):
            r, g, b = colorsys.hls_to_rgb(h, min(1, max(0, l)), s)
            r, g, b = ((c * v + 1 - v) if kind == "tint" else c * v for c in (r, g, b))
            h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, min(1, max(0, l)), s)
    return "%02X%02X%02X" % tuple(round(c * 255) for c in (r, g, b))


def _blend(top: str, bottom: str, alpha: float) -> str:
    return "%02X%02X%02X" % tuple(round(int(top[i:i + 2], 16) * alpha + int(bottom[i:i + 2], 16) * (1 - alpha))
                                  for i in (0, 2, 4))


def _lum(hexv: str) -> float:
    """WCAG relative luminance."""
    c = [int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(hex_a: str, hex_b: str) -> float:
    a, b = sorted((_lum(hex_a), _lum(hex_b)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def wants_light(bg: str, label: str | None = None) -> bool:
    """Does an icon on this color go lighter than it? The way its label goes when the label reads on it (a white
    heading on a teal header takes a white icon), else whichever of white and black stands out more on it."""
    if label and contrast(label, bg) >= LABEL_MIN_CONTRAST:
        return _lum(label) > _lum(bg)
    return contrast("FFFFFF", bg) > contrast("000000", bg)


def _fill(theme, props, group_fill=None):
    """(hex, opacity), None (no fill), IMAGE, or _UNSET for an spPr / bgPr / grpSpPr element."""
    for child in props if props is not None else []:
        kind = _name(child)
        if kind == "noFill":
            return None
        if kind == "solidFill":
            return theme.color(child[0])[::2] if len(child) else None
        if kind == "gradFill":
            stops = [theme.color(gs[0]) for gs in child.findall("a:gsLst/a:gs", NS) if len(gs)]
            return (_mix([s[0] for s in stops]), sum(s[2] for s in stops) / len(stops)) if stops else None
        if kind == "pattFill":
            fg = child.find("a:fgClr", NS)
            return theme.color(fg[0])[::2] if fg is not None and len(fg) else None
        if kind == "blipFill":
            return IMAGE
        if kind == "grpFill":
            return group_fill
    return _UNSET


def _mix(hexes):
    return "%02X%02X%02X" % tuple(round(sum(int(h[i:i + 2], 16) for h in hexes) / len(hexes)) for i in (0, 2, 4))


def _shape_fill(theme, el, group_fill):
    kind = _name(el)
    if kind == "pic":
        return IMAGE
    if kind != "sp":                       # lines, tables, charts: not treated as a background
        return None
    f = _fill(theme, el.find("p:spPr", NS), group_fill)
    if f is not _UNSET:
        return f
    ref = el.find("p:style/a:fillRef", NS)  # no fill of its own: the theme style decides
    if ref is not None and ref.get("idx", "0") != "0" and len(ref):
        return theme.color(ref[0])[::2]
    return None


def shape_fill(theme, sh):
    """A shape's own fill: (hex, opacity), None (no fill) or IMAGE."""
    return _shape_fill(theme, sh._element, None)


def is_drawn(sh, fill) -> bool:
    """Has visible edges: a picture, line, table or chart, or a shape with a fill or outline."""
    el = sh._element
    if _name(el) in ("pic", "graphicFrame", "cxnSp"):
        return True
    if fill is not None:
        return True
    ln = el.find("p:spPr/a:ln", NS)
    if ln is not None and ln.find("a:noFill", NS) is not None:
        return False
    if ln is not None and any(_name(c) in ("solidFill", "gradFill", "pattFill") for c in ln):
        return True
    ref = el.find("p:style/a:lnRef", NS)
    return ref is not None and ref.get("idx", "0") != "0"


RECTISH = {"rect", "roundRect", "snip1Rect", "snip2SameRect", "snip2DiagRect", "snipRoundRect", "round1Rect",
           "round2SameRect", "round2DiagRect", "flowChartProcess", "flowChartAlternateProcess", "line"}


def geometry(sh) -> str:
    """The shape's outline: a preset name (rect, hexagon, ellipse...), "custom", "picture", or "none"."""
    el = sh._element
    if _name(el) in ("pic", "graphicFrame"):
        return "picture" if _name(el) == "pic" else "table"
    if _name(el) == "cxnSp":
        return "line"
    prst = el.find("p:spPr/a:prstGeom", NS)
    return prst.get("prst", "rect") if prst is not None else ("custom" if el.find("p:spPr/a:custGeom", NS) is not None
                                                             else "none")


def _polygon(geom: str, rotation: float, box):
    """Corners of a preset hexagon, octagon or diamond within its on-screen box (turned a quarter at a time);
    None for anything else, whose box stands in for its outline."""
    quarters = (rotation or 0) / 90
    if abs(quarters - round(quarters)) > 0.02:
        return None
    turned = round(quarters) % 2 == 1
    l, t, r, b = box
    cx, cy, s = (l + r) / 2, (t + b) / 2, min(r - l, b - t)
    if geom == "hexagon":
        a = 0.25 * s
        return [(cx, t), (r, t + a), (r, b - a), (cx, b), (l, b - a), (l, t + a)] if turned else \
               [(l, cy), (l + a, t), (r - a, t), (r, cy), (r - a, b), (l + a, b)]
    if geom == "octagon":
        a = 0.29289 * s
        return [(l + a, t), (r - a, t), (r, t + a), (r, b - a), (r - a, b), (l + a, b), (l, b - a), (l, t + a)]
    if geom == "diamond":
        return [(cx, t), (r, cy), (cx, b), (l, cy)]
    return None


def inside_outline(geom: str, rotation: float, box, x: float, y: float) -> bool:
    """Is the point inside the shape's actual outline? Ellipses and preset polygons are tested exactly;
    rectangles, freeforms and the rest use their box."""
    l, t, r, b = box
    if geom == "ellipse":
        rx, ry = (r - l) / 2 or 1e-9, (b - t) / 2 or 1e-9
        return ((x - (l + r) / 2) / rx) ** 2 + ((y - (t + b) / 2) / ry) ** 2 <= 1.0
    poly = _polygon(geom, rotation, box)
    if poly is None:
        return l <= x <= r and t <= y <= b
    side = 0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if abs(cross) > 1e-12:
            if side and (cross > 0) != (side > 0):
                return False
            side = cross
    return True


def rotated_box(x, y, w, h, degrees):
    """(left, top, width, height) of the area a shape covers on screen once rotated about its center."""
    if not degrees % 180:
        return x, y, w, h
    rad = math.radians(degrees)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    nw, nh = w * c + h * s, w * s + h * c
    return x + (w - nw) / 2, y + (h - nh) / 2, nw, nh


def _child_tf(group, x, y, w, h, tf):
    """How a group at (x, y, w, h) on the slide maps its children's coordinates: (dx, dy, scale x, scale y)."""
    xfrm = group._element.grpSpPr.find("a:xfrm", NS)
    off = xfrm.find("a:chOff", NS) if xfrm is not None else None
    ext = xfrm.find("a:chExt", NS) if xfrm is not None else None
    if off is None or ext is None:   # no child frame: children already use the group's coordinates
        return tf
    csx, csy = w / (int(ext.get("cx")) or 1), h / (int(ext.get("cy")) or 1)
    return x - int(off.get("x")) * csx, y - int(off.get("y")) * csy, csx, csy


class Placed:
    """A shape inside a group, seen where it sits on the slide: left, top, width and height in slide EMU (group
    offsets and scaling applied, before its own rotation); `scale` is the group's horizontal scaling.
    Everything else is the shape's own."""

    def __init__(self, shape, left, top, width, height, scale=1.0):
        self._shape, self.left, self.top, self.width, self.height = shape, left, top, width, height
        self.scale = scale

    def __getattr__(self, name):
        return getattr(self._shape, name)


def leaves(shapes, tf=(0.0, 0.0, 1.0, 1.0), groups=False):
    """Every shape that isn't a group, in z-order, as placed on the slide (a Placed when inside a group);
    with groups=True, each group too (as Placed), before its children."""
    ox, oy, sx, sy = tf
    for sh in shapes:
        if sh.left is None or sh.width is None:
            continue
        if tf == (0.0, 0.0, 1.0, 1.0) and sh.shape_type != MSO_SHAPE_TYPE.GROUP:
            yield sh
            continue
        x, y, w, h = ox + sh.left * sx, oy + sh.top * sy, sh.width * sx, sh.height * sy
        placed = Placed(sh, round(x), round(y), round(w), round(h), sx)
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            if groups:
                yield placed
            yield from leaves(sh.shapes, _child_tf(sh, x, y, w, h, tf), groups)
        else:
            yield placed


def find_shape(shapes, shape_id):
    """The shape with this id, inside groups too (as placed on the slide), or None."""
    return next((s for s in leaves(shapes) if s.shape_id == shape_id), None)


def _frames(theme, shapes, tf=(0.0, 0.0, 1.0, 1.0), group_fill=None):
    """Every shape that isn't a group, in z-order: ((left, top, width, height) in slide EMU before its own
    rotation, its rotation, the fill its group passes down, the shape)."""
    ox, oy, sx, sy = tf
    for sh in shapes:
        if sh.left is None or sh.width is None:
            continue
        x, y, w, h = ox + sh.left * sx, oy + sh.top * sy, sh.width * sx, sh.height * sy
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            own = _fill(theme, sh._element.grpSpPr, group_fill)
            yield from _frames(theme, sh.shapes, _child_tf(sh, x, y, w, h, tf), group_fill if own is _UNSET else own)
        else:
            yield (x, y, w, h), getattr(sh, "rotation", 0) or 0, group_fill, sh


def boxes(theme, shapes, tf=(0.0, 0.0, 1.0, 1.0), group_fill=None):
    """(left, top, right, bottom) in EMU, fill, and shape for every shape in z-order; group children on
    slide coordinates."""
    for (x, y, w, h), rotation, passed, sh in _frames(theme, shapes, tf, group_fill):
        x, y, w, h = rotated_box(x, y, w, h, rotation)
        yield (x, y, x + w, y + h), _shape_fill(theme, sh._element, passed), sh


FILLS = {"noFill", "solidFill", "gradFill", "pattFill", "blipFill", "grpFill"}
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_rasters = weakref.WeakKeyDictionary()   # image part -> small RGBA copy (None: unreadable), for the deck's lifetime


def _local(frame, rotation, el, x, y):
    """A slide point as shares of a shape's own frame (0 to 1 across and down), its rotation and flips undone."""
    fx, fy, fw, fh = frame
    if rotation:
        cx, cy, a = fx + fw / 2, fy + fh / 2, math.radians(-rotation)
        dx, dy = x - cx, y - cy
        x, y = cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)
    u, v = (x - fx) / (fw or 1), (y - fy) / (fh or 1)
    xfrm = el.find("p:spPr/a:xfrm", NS)
    if xfrm is not None:
        u = 1 - u if xfrm.get("flipH") in ("1", "true") else u
        v = 1 - v if xfrm.get("flipV") in ("1", "true") else v
    return u, v


def _gradient_at(theme, grad, u, v):
    """(hex, opacity) a gradient paints at a point of its shape (u, v as shares of its frame), or None without
    stops. A linear gradient runs along its angle; a radial or rectangular one runs out from its focus (first stop)
    to the frame's corners. Averaging the stops instead misreads a dark badge with a light rim as pale."""
    stops = sorted(((int(gs.get("pos", "0")) / 100000, theme.color(gs[0])) for gs in grad.findall("a:gsLst/a:gs", NS)
                    if len(gs)), key=lambda s: s[0])
    if not stops:
        return None
    path = grad.find("a:path", NS)
    if path is not None:
        rect = path.find("a:fillToRect", NS)
        l, t, r, b = ((int(rect.get(k, "0")) / 100000 if rect is not None else 0.0) for k in "ltrb")
        x0, x1, y0, y1 = l, 1 - r, t, 1 - b   # the focus
        dx = (x0 - u) / x0 if u < x0 and x0 > 0 else ((u - x1) / (1 - x1) if u > x1 and x1 < 1 else 0.0)
        dy = (y0 - v) / y0 if v < y0 and y0 > 0 else ((v - y1) / (1 - y1) if v > y1 and y1 < 1 else 0.0)
        pos = math.hypot(dx, dy) / math.sqrt(2) if path.get("path") == "circle" else max(dx, dy)
    else:
        lin = grad.find("a:lin", NS)
        a = math.radians(int(lin.get("ang", "0")) / 60000 if lin is not None else 0)
        c, s = math.cos(a), math.sin(a)
        pos = 0.5 + ((u - 0.5) * c + (v - 0.5) * s) / ((abs(c) + abs(s)) or 1)
    pos = min(1.0, max(0.0, pos))
    if pos <= stops[0][0] or len(stops) == 1:
        return stops[0][1][0], stops[0][1][2]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if pos <= p1:
            k = (pos - p0) / (p1 - p0) if p1 > p0 else 1.0
            return _blend(c1[0], c0[0], k), c0[2] + (c1[2] - c0[2]) * k
    return stops[-1][1][0], stops[-1][1][2]


def _raster(part):
    """A small RGBA copy of an image part, or None when it can't be read (a metafile, say)."""
    if part not in _rasters:
        from io import BytesIO
        from PIL import Image
        try:
            im = Image.open(BytesIO(part.blob))
            im.draft("RGB", (256, 256))   # a JPEG decodes at a reduced size
            im = im.convert("RGBA")
            im.thumbnail((128, 128))
        except Exception:   # Pillow raises many kinds for formats it can't read
            im = None
        _rasters[part] = im
    return _rasters[part]


def _picture_at(part, blip_fill, u, v):
    """(hex, opacity) a picture shows at a point of its frame (u, v as shares of it), cropping and transparency
    applied: its pixels around that point, averaged. None where it is see-through or cropped away; IMAGE when it
    can't be read."""
    blip = blip_fill.find("a:blip", NS) if blip_fill is not None else None
    try:
        im = _raster(part.related_part(blip.get(R_EMBED)))
    except (AttributeError, KeyError, TypeError):
        im = None
    if im is None:
        return IMAGE
    crop = blip_fill.find("a:srcRect", NS)
    l, t, r, b = ((int(crop.get(k, "0")) / 100000 if crop is not None else 0.0) for k in "ltrb")
    fx, fy = l + u * (1 - l - r), t + v * (1 - t - b)
    if not (0 <= fx <= 1 and 0 <= fy <= 1):
        return None
    w, h = im.size
    x, y, k = min(w - 1, int(fx * w)), min(h - 1, int(fy * h)), max(1, round(0.02 * max(w, h)))
    px = [im.getpixel((i, j)) for i in range(max(0, x - k), min(w, x + k + 1))
          for j in range(max(0, y - k), min(h, y + k + 1))]
    weight = sum(p[3] for p in px)
    fix = blip.find("a:alphaModFix", NS)
    alpha = weight / (255 * len(px)) * (int(fix.get("amt", "100000")) / 100000 if fix is not None else 1.0)
    if alpha <= 0.02:
        return None
    return "%02X%02X%02X" % tuple(round(sum(p[c] * p[3] for p in px) / weight) for c in range(3)), alpha


def _bezier(ctrl, t):
    while len(ctrl) > 1:
        ctrl = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for a, b in zip(ctrl, ctrl[1:])]
    return ctrl[0]


def _outlines(path) -> list[list[tuple]]:
    """A freeform path's subpaths as polygons in its own coordinates, curves and arcs flattened. Raises
    ValueError or TypeError on formula-driven points (guide names), which aren't read."""
    polys, cur, pos = [], [], (0.0, 0.0)
    for cmd in path:
        kind = _name(cmd)
        pts = [(float(p.get("x")), float(p.get("y"))) for p in cmd.findall("a:pt", NS)]
        if kind in ("moveTo", "close"):
            if len(cur) > 2:
                polys.append(cur)
            pos = pts[0] if kind == "moveTo" else (cur[0] if cur else pos)
            cur = [pos] if kind == "moveTo" else []
            continue
        cur = cur or [pos]
        if kind == "lnTo":
            cur.append(pts[0])
        elif kind in ("cubicBezTo", "quadBezTo"):
            cur += [_bezier([pos] + pts, i / 8) for i in range(1, 9)]
        elif kind == "arcTo":
            wr, hr = float(cmd.get("wR")), float(cmd.get("hR"))
            st, sw = math.radians(float(cmd.get("stAng")) / 60000), math.radians(float(cmd.get("swAng")) / 60000)
            cx, cy = pos[0] - wr * math.cos(st), pos[1] - hr * math.sin(st)
            cur += [(cx + wr * math.cos(st + sw * i / 12), cy + hr * math.sin(st + sw * i / 12)) for i in range(1, 13)]
        pos = cur[-1]
    if len(cur) > 2:
        polys.append(cur)
    return polys


def _in_freeform(el, u, v) -> bool:
    """Is a point (shares of the shape's frame) on a freeform's filled area? Even-odd, as PowerPoint fills it, so
    the hole of a ring drawn as one freeform is not. True when the outline can't be read (its box stands in)."""
    ext = el.find("p:spPr/a:xfrm/a:ext", NS)
    paths = el.findall("p:spPr/a:custGeom/a:pathLst/a:path", NS)
    try:
        for path in paths:
            if path.get("fill") == "none":
                continue
            x, y = u * float(path.get("w") or ext.get("cx")), v * float(path.get("h") or ext.get("cy"))
            inside = False
            for poly in _outlines(path):
                for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
                    if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                        inside = not inside
            if inside:
                return True
    except (AttributeError, IndexError, TypeError, ValueError):
        return True
    return not paths


def _paint_at(theme, frame, rotation, passed, sh, x, y):
    """What a shape paints at a slide point (EMU): (hex, opacity); None (no fill, or the point is outside its
    outline); or IMAGE (a picture that can't be read). Gradients and pictures are read where the point is."""
    el = sh._element
    kind = _name(el)
    if kind not in ("sp", "pic"):            # lines, tables, charts: not treated as a background
        return None
    l, t, w, h = rotated_box(*frame, rotation)
    geom = geometry(sh)
    if not inside_outline(geom, rotation, (l, t, l + w, t + h), x, y):
        return None
    u, v = _local(frame, rotation, el, x, y)
    if geom == "custom" and not _in_freeform(el, u, v):
        return None
    if kind == "pic":
        return _picture_at(sh.part, el.find("p:blipFill", NS), u, v)
    props = el.find("p:spPr", NS)
    fill = next((c for c in (props if props is not None else []) if _name(c) in FILLS), None)
    if fill is not None and _name(fill) == "gradFill":
        return _gradient_at(theme, fill, u, v)
    if fill is not None and _name(fill) == "blipFill":
        return _picture_at(sh.part, fill, u, v)
    return _shape_fill(theme, el, passed)


def _background_at(theme, slide, x, y):
    """Hex or IMAGE at a slide point (EMU): the slide's background, else its layout's, else its master's, else
    bg1. A gradient or picture background is read where the point is."""
    pres = slide.part.package.presentation_part.presentation
    u, v = x / (pres.slide_width or 1), y / (pres.slide_height or 1)
    for part in (slide, slide.slide_layout, slide.slide_layout.slide_master):
        bg = part._element.find("p:cSld/p:bg", NS)
        if bg is None:
            continue
        props = bg.find("p:bgPr", NS)
        fill = next((c for c in (props if props is not None else []) if _name(c) in FILLS), None)
        kind = _name(fill) if fill is not None else None
        f = (_gradient_at(theme, fill, u, v) if kind == "gradFill" else
             _picture_at(part.part, fill, u, v) if kind == "blipFill" else _fill(theme, props))
        if f == IMAGE:
            return IMAGE
        if f is not _UNSET and f is not None:
            return _blend(f[0], "FFFFFF", f[1])
        ref = bg.find("p:bgRef", NS)
        if ref is not None and len(ref):
            return theme.color(ref[0])[0]
    return theme.slot_hex("bg1")


def layers(slide) -> list[tuple[list, bool]]:
    """What draws on a slide, back to front, as (shapes, on the slide itself?): the master's and layout's
    decorations (lines, bands, logos; not their placeholders) unless the slide hides them, then its own shapes."""
    out = []
    layout = slide.slide_layout
    if slide._element.get("showMasterSp", "1") != "0":
        if layout._element.get("showMasterSp", "1") != "0":
            out.append(([s for s in layout.slide_master.shapes if not s.is_placeholder], False))
        out.append(([s for s in layout.shapes if not s.is_placeholder], False))
    out.append((list(slide.shapes), True))
    return out


def _surfaces(theme, slide, ignore=()):
    """What draws on a slide, back to front, as _frames entries. `ignore`: ids of the slide's own shapes to leave
    out (a placeholder the icon replaces)."""
    return [e for shapes, own in layers(slide) for e in _frames(theme, shapes) if not (own and e[3].shape_id in ignore)]


def _color_at(theme, slide, surfaces, x, y):
    """(hex or IMAGE, what it is) at a slide point (EMU): the background with every filled shape and picture
    under the point composited on top in z-order."""
    color, where = _background_at(theme, slide, x, y), "the slide background"
    for frame, rotation, passed, sh in surfaces:
        paint = _paint_at(theme, frame, rotation, passed, sh, x, y)
        if paint is None:
            continue
        if paint == IMAGE:
            color, where = IMAGE, "a picture"
            continue
        hexv, alpha = paint
        if alpha >= 0.999 or (color == IMAGE and alpha >= 0.5):  # mostly opaque over a picture: the fill dominates
            color = hexv
        elif color != IMAGE:
            color = _blend(hexv, color, alpha)
        where = "a picture" if _name(sh._element) == "pic" else "the shape behind it"
    return color, where


def behind(theme, slide, x_in, y_in, ignore=()):
    """(hex or IMAGE, what it is) at a point (inches): the background with every filled shape and picture under
    the point composited on top in z-order. `ignore`: ids of the slide's own shapes to leave out (a placeholder
    the icon replaces)."""
    return _color_at(theme, slide, _surfaces(theme, slide, ignore), x_in * EMU, y_in * EMU)


SAMPLES = (0.2, 0.5, 0.8)   # where across and down an icon's box its background is read (its drawing, not corners)


def behind_icon(theme, slide, left, top, size, ignore=()):
    """(hex or IMAGE, what it is) under each part of an icon's box (inches): a 3x3 grid over where its drawing
    is, so an icon on a badge, a gradient or a photo is judged on all it covers, not one point."""
    surfaces = _surfaces(theme, slide, ignore)
    return [_color_at(theme, slide, surfaces, (left + size * a) * EMU, (top + size * b) * EMU)
            for b in SAMPLES for a in SAMPLES]


NEAR = 40               # RGB distance within which an existing icon's color counts as a theme color
CLASS_SLOT = {name: slot for slot, name in THEME_CLASS.items()}


def near_slot(theme, hexv: str) -> str | None:
    """The theme slot closest to a color, if it is close enough to be meant as that theme color."""
    rgb = [int(hexv[i:i + 2], 16) for i in (0, 2, 4)]
    def dist(slot):
        h = theme.slot_hex(slot)
        return sum((a - int(h[i:i + 2], 16)) ** 2 for a, i in zip(rgb, (0, 2, 4))) ** 0.5
    best = min(THEME_CLASS, key=dist)
    return best if dist(best) <= NEAR else None


def icon_slot(theme, shape) -> str | None:
    """The theme slot an existing icon is drawn in: its SVG's theme class, else its main color (SVG paint,
    PNG pixels, or the fills and outlines of a native icon) when that is a theme color. None otherwise."""
    from .icon_tools import NS_SVG, NS_R, svg_paint
    el = shape._element
    counts = Counter()
    if _name(el) == "pic":
        svg_blip = el.find(".//{%s}svgBlip" % NS_SVG)
        try:
            if svg_blip is not None:
                svg = shape.part.related_part(svg_blip.get("{%s}embed" % NS_R)).blob
                named = Counter(m.decode() for m in re.findall(rb"MsftOfcThm_(\w+?)_(?:Fill|Stroke)_v2", svg))
                if named:
                    return CLASS_SLOT.get(named.most_common(1)[0][0])
                counts = svg_paint(svg)
            else:
                counts = _pixel_colors(shape.image.blob)
        except (KeyError, ValueError, OSError, etree.XMLSyntaxError):
            return None
    else:  # a group or freeform drawn with shapes: the colors of its fills and outlines, not its text
        for fill in el.iter("{%s}solidFill" % NS["a"]):
            if _name(fill.getparent()) in ("spPr", "ln") and len(fill):
                hexv, slot, _ = theme.color(fill[0])
                counts[theme.slot_hex(slot) if slot else hexv] += 1
    if not counts:
        return None
    return near_slot(theme, counts.most_common(1)[0][0])


def _pixel_colors(blob) -> Counter:
    """The main colors of a raster icon: its opaque pixels, or on an opaque image the pixels that differ
    from the border (the background)."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(blob)).convert("RGBA")
    im.thumbnail((64, 64))
    raw = im.tobytes()
    px = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]
    if min(p[3] for p in px) < 250:
        px = [p for p in px if p[3] >= 250]
    else:
        w, h = im.size
        edge = Counter(im.getpixel((x, y))[:3] for x in range(w) for y in (0, h - 1)).most_common(1)[0][0]
        px = [p for p in px if sum(abs(a - b) for a, b in zip(p[:3], edge)) > 60]
    return Counter("%02X%02X%02X" % p[:3] for p in px)


def text_color(theme, slide, item) -> tuple[str, str | None] | None:
    """(hex, theme slot or None) of the item's text color, when the slide sets one: the run's color, else the
    box's list style, else the shape style's font color. None when it's inherited from the master (not read), so
    an assumed color never counts as the designer's choice."""
    shape = find_shape(slide.shapes, item.shape_id)
    if shape is None or not shape.has_text_frame:
        return None
    paras = [p for p in shape.text_frame.paragraphs if p.text.strip()]
    para = paras[item.paragraph] if item.paragraph < len(paras) else (paras[0] if paras else None)
    runs = [r for r in (para.runs if para else []) if r.text.strip()]
    sources = [r._r.find("a:rPr/a:solidFill", NS) for r in runs[:1]] + [
        shape._element.find("p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr/a:solidFill", NS),
        shape._element.find("p:style/a:fontRef", NS)]
    for el in sources:
        if el is not None and len(el):
            hexv, slot, _ = theme.color(el[0])
            return hexv, slot
    return None


def text_slot(theme, slide, item) -> str | None:
    """The theme slot of the item's text color (text_color), or None when it isn't set or isn't a theme color."""
    found = text_color(theme, slide, item)
    return found[1] if found else None


def assign(prs, picks, default_slot: str = "accent1") -> None:
    """Set color_slot, color_hex and color_note on each pick. Icons in one set share one color, unless the
    designer colored the set's labels differently per background: then each icon matches its own label.

    A color reads on a background when it has the contrast and goes the way a designer would: lighter than a
    surface that takes a light icon (its label is light there, or white stands out more than black), darker than
    one that takes a dark icon. So black on a dark teal header loses to white even where it reaches 3:1."""
    themes = {}
    sets = defaultdict(list)
    for p in picks:
        sets[p.item.set_id or f"solo-{id(p)}"].append(p)
    for members in sets.values():
        seen = []  # (pick, [(background hex or IMAGE, what it is)] under the icon, label's (hex, slot) or None)
        for p in members:
            slide = prs.slides[p.item.slide - 1]
            master = slide.slide_layout.slide_master
            theme = themes.setdefault(id(master), Theme(master))
            ignore = set(p.item.placeholder["ids"]) if p.item.placeholder else ()   # the logo the icon replaces
            seen.append((p, behind_icon(theme, slide, p.left, p.top, p.size, ignore), text_color(theme, slide, p.item)))
        known = [(bg, where, label[0] if label else None) for _, under, label in seen for bg, where in under
                 if bg != IMAGE]
        own = [label[1] if label else None for *_, label in seen]
        labels = [s for s in own if s in THEME_CLASS and s != default_slot]
        # The designer colored the placeholders: their theme color comes first, when it reads.
        held = Counter(p.item.placeholder["slot"] for p in members if p.item.placeholder
                       and p.item.placeholder.get("slot") in THEME_CLASS)
        candidates = [(s, MIN_CONTRAST, "the placeholder's color") for s, _ in held.most_common(1)]
        candidates += [(default_slot, MIN_CONTRAST, "deck color")]
        candidates += [(s, LABEL_MIN_CONTRAST, "the label's color") for s in dict.fromkeys(labels)]
        candidates += [(s, MIN_CONTRAST, "theme dark/light") for s in FALLBACK_SLOTS if s != default_slot]

        def worst(slot):
            return min((contrast(theme.slot_hex(slot), bg), bg, where) for bg, where, _ in known)

        def clash(slot):
            """(background, what it is) where the slot goes the wrong way (dark where the icon should be light, or
            the reverse), or None."""
            hexv = theme.slot_hex(slot)
            return next(((bg, where) for bg, where, label in known
                         if (_lum(hexv) > _lum(bg)) != wants_light(bg, label)), None)

        def reads(slot, need):
            return worst(slot)[0] >= need and clash(slot) is None

        def shortfall(slot):
            ratio, bg, where = worst(slot)
            if ratio < MIN_CONTRAST:
                return f"is only {ratio:.1f}:1 on {where} (#{bg})"
            bg, where = clash(slot)
            light = _lum(theme.slot_hex(slot)) > _lum(bg)
            return (f"would be a {'light' if light else 'dark'} icon on {where} (#{bg}), which takes a "
                    f"{'dark' if light else 'light'} one")

        # The designer gave the labels different colors for their backgrounds (white heading on a dark panel,
        # dark heading on a light one): when the deck color doesn't work for all, each icon matches its label.
        placeholder_works = held and (not known or reads(candidates[0][0], MIN_CONTRAST))
        if known and not placeholder_works and not reads(default_slot, MIN_CONTRAST) and len(set(own)) > 1 \
                and all(s in THEME_CLASS for s in own) \
                and all(bg == IMAGE or contrast(theme.slot_hex(s), bg) >= LABEL_MIN_CONTRAST
                        for (_, under, _), s in zip(seen, own) for bg, _ in under):
            for (p, under, _), s in zip(seen, own):
                mine = [(contrast(theme.slot_hex(s), bg), bg, where) for bg, where in under if bg != IMAGE]
                ratio, bg, where = min(mine) if mine else (None, None, None)
                p.color_slot, p.color_hex = s, theme.slot_hex(s)
                p.color_note = (f"{s} (matches its label): the labels in this set use different colors for their "
                                f"backgrounds" + (f"; {ratio:.1f}:1 on {where} (#{bg})" if ratio else ""))
            continue
        if not known:
            slot, why = candidates[0][0], candidates[0][2]
            note = f"{slot} ({why}): sits on a picture that can't be read, so contrast wasn't checked"
        else:
            chosen = next((c for c in candidates if reads(c[0], c[1])), None)
            slot, _, why = chosen or max(candidates, key=lambda c: worst(c[0])[0])
            ratio, bg, where = worst(slot)
            if slot == default_slot or (chosen and why == "the placeholder's color"):
                note = f"{slot} ({why}): {ratio:.1f}:1 on {where} (#{bg})"
            else:
                note = f"{slot} ({why}): the deck color {default_slot} {shortfall(default_slot)}; {slot} is {ratio:.1f}:1"
            if not chosen:
                note += ", still low: check this slide"
        for p, *_ in seen:
            p.color_slot, p.color_hex, p.color_note = slot, theme.slot_hex(slot), note
