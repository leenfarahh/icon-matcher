"""PL logo placeholders: the logo a designer drops where an icon should go. The icon replaces it, taking its
place, size, group and z-order.

A placeholder is found by its look: the same shapes as a reference copy in the library's placeholders/ folder
(any .pptx; each shape on its slides is one reference), at any position, size or color. Or by its name: a shape
named or alt-texted "PL", "PL logo", "Prezlab logo" or "Icon placeholder" (Selection Pane or Alt Text)."""
import hashlib
import math
import re
from pathlib import Path

from lxml import etree
from pptx import Presentation

from . import colors

FOLDER = "placeholders"   # in the library folder
TOLERANCE = 0.03          # two copies match when every share of their boxes (and log of proportions) is this close
NAME = re.compile(r"^\s*(pl|pl logo|prezlab logo|icon placeholder)(\s+\d+)?\s*$", re.I)
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
SVG = "{http://schemas.microsoft.com/office/drawing/2016/SVG/main}"
DRAWN = {"sp", "grpSp", "pic", "cxnSp"}
SVG_SHAPES = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
SVG_HIDDEN = {"defs", "clipPath", "mask", "symbol", "pattern", "marker", "metadata"}
SVG_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
SVG_ARGS = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "S": 4, "Q": 4, "C": 6, "A": 7}   # numbers per path command


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _xfrm(el):
    return el.find(f"{P}grpSpPr/{A}xfrm" if _tag(el) == "grpSp" else f"{P}spPr/{A}xfrm")


def _frame(el, child=False):
    """(x, y, w, h) of an element in its parent's coordinates; with child=True, a group's own child frame."""
    xfrm = _xfrm(el)
    off, ext = (xfrm.find(f"{A}chOff"), xfrm.find(f"{A}chExt")) if xfrm is not None and child else (None, None)
    if off is None or ext is None:
        off, ext = (xfrm.find(f"{A}off"), xfrm.find(f"{A}ext")) if xfrm is not None else (None, None)
    if off is None or ext is None:
        return None
    return float(off.get("x")), float(off.get("y")), float(ext.get("cx")), float(ext.get("cy"))


def _proportion(w, h) -> float:
    return round(math.log(w / h), 4) if w > 0 and h > 0 else 0.0


def _number(v, scale):
    try:
        return float(v) / scale
    except (TypeError, ValueError):
        return v   # a guide name in a formula-driven path: compared as is


def _svg_number(v) -> float:
    """An SVG length's number ("300", "300px"), or 0."""
    m = SVG_TOKEN.match((v or "").strip())
    try:
        return float(m.group()) if m else 0.0
    except ValueError:
        return 0.0


def _path(d: str) -> list[tuple]:
    """SVG path data as absolute segments: M, L, C, Q, A (endpoint last) and Z. H and V become lines, S and T
    curves with their reflected control point, so one outline reads the same however it is written: PowerPoint
    rewrites an inserted SVG's relative and shorthand commands as absolute ones."""
    tokens, out = SVG_TOKEN.findall(d), []
    x = y = sx = sy = 0.0
    cmd, ctrl, i = None, None, 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd, i = tokens[i], i + 1
            if cmd in "Zz":
                out.append(("Z",))
                x, y, ctrl = sx, sy, None
                continue
        n = SVG_ARGS.get((cmd or "Z").upper())
        if not n or i + n > len(tokens) or any(t.isalpha() for t in tokens[i:i + n]):
            break   # malformed: what was read so far stands for the path
        v, i = [float(t) for t in tokens[i:i + n]], i + n
        ox, oy = (x, y) if cmd.islower() else (0.0, 0.0)
        kind = cmd.upper()
        if kind == "H":
            kind, v = "L", [v[0] + ox, y]
        elif kind == "V":
            kind, v = "L", [x, v[0] + oy]
        elif kind == "A":
            v[5:] = [v[5] + ox, v[6] + oy]
        else:
            v = [c + (oy if k % 2 else ox) for k, c in enumerate(v)]
        if kind in "ST":
            family = "C" if kind == "S" else "Q"
            first = [2 * x - ctrl[1], 2 * y - ctrl[2]] if ctrl and ctrl[0] == family else [x, y]
            kind, v = family, first + v
        ctrl = ("C", v[2], v[3]) if kind == "C" else ("Q", v[0], v[1]) if kind == "Q" else None
        if kind == "M":
            sx, sy = v
            cmd = "l" if cmd == "m" else "L"   # more pairs after a move are lines
        out.append((kind, *v))
        x, y = v[-2], v[-1]
    return out


def _drawing(svg: bytes):
    """What an SVG draws: each visible shape in order, a path as its absolute segments with points as shares of
    the drawing's extent, other shapes by kind and proportions. Not its colors, ids, metadata or how its numbers
    are written, so a copy PowerPoint rewrote on insert, recolored, or issued with other provenance data still
    matches (compared with `same`)."""
    try:
        root = etree.fromstring(svg, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return hashlib.sha1(svg).hexdigest()
    shapes = []
    for e in root.iter():
        kind = _tag(e) if isinstance(e.tag, str) else None
        if kind not in SVG_SHAPES or any(_tag(a) in SVG_HIDDEN for a in e.iterancestors()):
            continue
        if kind in ("path", "polygon", "polyline"):
            d = e.get("d", "") if kind == "path" else "M" + e.get("points", "") + ("Z" if kind == "polygon" else "")
            shapes.append(("path", _path(d)))
        elif kind in ("rect", "ellipse"):
            a, b = ("width", "height") if kind == "rect" else ("rx", "ry")
            shapes.append((kind, _proportion(_svg_number(e.get(a)), _svg_number(e.get(b)))))
        else:
            shapes.append((kind,))
    if not shapes:
        return hashlib.sha1(svg).hexdigest()   # nothing drawn: the file itself
    points = [p for s in shapes if s[0] == "path" for seg in s[1]
              for p in ([seg[6:8]] if seg[0] == "A" else [seg[k:k + 2] for k in range(1, len(seg), 2)])]
    x0, y0 = min((p[0] for p in points), default=0.0), min((p[1] for p in points), default=0.0)
    span = max([p[0] - x0 for p in points] + [p[1] - y0 for p in points] + [0.0]) or 1.0

    def share(seg):
        if seg[0] == "A":   # radii scale with the drawing; rotation and flags stay
            return ("A", seg[1] / span, seg[2] / span, *seg[3:6], (seg[6] - x0) / span, (seg[7] - y0) / span)
        return (seg[0], *((c - (y0 if k % 2 else x0)) / span for k, c in enumerate(seg[1:])))
    return tuple(("path", tuple(share(seg) for seg in s[1])) if s[0] == "path" else s for s in shapes)


def _sig(el, part):
    tag = _tag(el)
    if tag == "grpSp":
        frame = _frame(el, child=True)
        if frame is None:
            return ("grpSp",)
        ox, oy, cw, ch = frame
        parts = []
        for c in el:
            f = _frame(c) if _tag(c) in DRAWN else None
            if f:
                x, y, w, h = f
                parts.append(((x - ox) / (cw or 1), (y - oy) / (ch or 1), w / (cw or 1), h / (ch or 1), _sig(c, part)))
        return ("grpSp", _proportion(cw, ch), tuple(sorted(parts, key=lambda p: (p[0], p[1]))))
    frame = _frame(el)
    proportion = _proportion(frame[2], frame[3]) if frame else 0.0
    if tag == "pic":
        svg = el.find(f".//{SVG}svgBlip")
        blip = svg if svg is not None else el.find(f".//{A}blip")
        rid = blip.get(f"{R}embed") if blip is not None else None
        try:
            blob = part.related_part(rid).blob if rid and part is not None else b""
        except KeyError:
            blob = b""
        digest = (_drawing(blob) if svg is not None else hashlib.sha1(blob).hexdigest()) if blob else ""
        return ("pic", proportion, digest)
    prst = el.find(f"{P}spPr/{A}prstGeom")
    if prst is not None:
        return (tag, proportion, prst.get("prst"))
    paths = []
    for path in el.iter(f"{A}path"):
        pw, ph = float(path.get("w") or (frame[2] if frame else 1) or 1), float(path.get("h") or (frame[3] if frame else 1) or 1)
        cmds = []
        for cmd in path:
            name = _tag(cmd)
            if name == "arcTo":
                vals = (_number(cmd.get("wR"), pw), _number(cmd.get("hR"), ph),
                        _number(cmd.get("stAng"), 21600000), _number(cmd.get("swAng"), 21600000))
            else:
                vals = tuple(v for pt in cmd.iter(f"{A}pt") for v in (_number(pt.get("x"), pw), _number(pt.get("y"), ph)))
            cmds.append((name, vals))
        paths.append(tuple(cmds))
    return (tag, proportion, tuple(paths))


def signature(shape) -> tuple:
    """What a shape looks like, apart from where it sits, its size, color and name: its outline (preset name, or
    freeform path as shares of its box) and proportions; for a group, each part's look and place in the group;
    for a picture, its image (for an SVG, the shapes it draws). Copies of one logo share it however they are
    moved, resized or recolored."""
    return _sig(shape._element, getattr(shape, "part", None))


def same(a, b, tol: float = TOLERANCE) -> bool:
    """Two signatures match: the same structure, and every number within tol."""
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= tol
    return a == b


def load_refs(folder: Path) -> tuple[list[tuple], list[str]]:
    """The reference signatures in a folder of .pptx files (each shape on each slide is one), and warnings."""
    refs, warnings = [], []
    for path in sorted(Path(folder).glob("*.pptx")) if Path(folder).is_dir() else []:
        try:
            prs = Presentation(path)
        except Exception as e:   # a damaged or locked file shouldn't stop the library from loading
            warnings.append(f"placeholder reference {path.name} can't be read ({e})")
            continue
        refs += [signature(s) for slide in prs.slides for s in slide.shapes if _tag(s._element) in DRAWN]
    return refs, warnings


def is_placeholder(shape, refs) -> bool:
    """Named as one, or the same look as a reference (cheap checks first: kind and proportions)."""
    el = shape._element
    c_nv = el.find(f".//{P}cNvPr")
    if c_nv is not None and (NAME.match(c_nv.get("name", "")) or NAME.match(c_nv.get("descr", ""))):
        return True
    if not refs or _tag(el) not in DRAWN:
        return False
    frame = _frame(el, child=_tag(el) == "grpSp")
    proportion = _proportion(frame[2], frame[3]) if frame else 0.0
    candidates = [r for r in refs if r[0] == _tag(el) and len(r) > 1 and abs(r[1] - proportion) <= TOLERANCE]
    return bool(candidates) and any(same(signature(shape), r) for r in candidates)


def find(slide, refs) -> list:
    """The placeholders on a slide, inside groups too, as placed on the slide (a group that is one is not
    searched further)."""
    found, inside = [], set()
    for sh in colors.leaves(slide.shapes, groups=True):
        if sh.shape_id in inside:
            continue
        if is_placeholder(sh, refs):
            found.append(sh)
            inside |= {int(e.get("id")) for e in sh._element.iter(f"{P}cNvPr")}
    return found


def replace(slide, shape_id: int, pic) -> bool:
    """Put an inserted icon in a placeholder's place: the same parent group and z-order, centered in the
    placeholder's frame at the largest size that keeps the icon's proportions; then remove the placeholder.
    False when the placeholder isn't on the slide (the icon then stays where it was inserted)."""
    target = next((s for s in colors.leaves(slide.shapes, groups=True) if s.shape_id == shape_id), None)
    frame = _frame(target._element) if target is not None else None
    if frame is None:
        return False
    x, y, cw, ch = frame                                     # in the placeholder's parent's coordinates
    sx, sy = target.width / (cw or 1), target.height / (ch or 1)   # parent units -> slide EMU
    aspect = pic.width / pic.height if pic.height else 1.0
    w, h = target.width, target.height
    iw, ih = (w, w / aspect) if w / h <= aspect else (h * aspect, h)   # on the slide, proportions kept
    pw, ph = iw / sx, ih / sy
    xfrm = pic._element.spPr.find(f"{A}xfrm")
    xfrm.find(f"{A}off").set("x", str(round(x + (cw - pw) / 2)))
    xfrm.find(f"{A}off").set("y", str(round(y + (ch - ph) / 2)))
    xfrm.find(f"{A}ext").set("cx", str(round(pw)))
    xfrm.find(f"{A}ext").set("cy", str(round(ph)))
    el = target._element
    parent = el.getparent()
    parent.insert(parent.index(el), pic._element)            # moves the icon from the end of the slide
    parent.remove(el)
    return True
