"""Low-level .pptx icon insertion (same helper as the prezlab-icon-placement skill)."""
import io, re, json, copy
from lxml import etree
from pptx import Presentation
from pptx.util import Emu, Inches
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI

EMU = 914400
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_SVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
SVG_EXT = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
RT_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

def inventory(path):
    """Every slide's shapes with text, position (inches) and an icon guess."""
    prs = Presentation(path)
    out = {"slide_w": prs.slide_width / EMU, "slide_h": prs.slide_height / EMU, "slides": []}
    def walk(shapes, depth=0):
        for sh in shapes:
            if sh.left is None:
                continue
            w, h = sh.width / EMU, sh.height / EMU
            xml = etree.tostring(sh._element).decode()
            rec = {"id": sh.shape_id, "name": sh.name, "depth": depth,
                   "left": round(sh.left / EMU, 3), "top": round(sh.top / EMU, 3),
                   "w": round(w, 3), "h": round(h, 3),
                   "text": sh.text_frame.text.strip() if sh.has_text_frame else "",
                   "kind": sh.shape_type and str(sh.shape_type).split(".")[-1].split(" ")[0]}
            small_square = 0 < w <= 1.6 and 0 < h <= 1.6 and 0.6 <= (w / h if h else 0) <= 1.6
            rec["icon_like"] = bool(small_square and not rec["text"] and
                                    ("svgBlip" in xml or "<p:pic" in xml or "custGeom" in xml or "grpSp" in xml))
            yield rec
            if sh.shape_type is not None and "GROUP" in str(sh.shape_type):
                yield from walk(sh.shapes, depth + 1)
    for i, s in enumerate(prs.slides, 1):
        out["slides"].append({"n": i, "layout": s.slide_layout.name, "shapes": list(walk(s.shapes))})
    return out

# Theme slots an icon may take, and PowerPoint's class names for them: an SVG whose shapes carry
# MsftOfcThm_<Name>_Fill_v2 / _Stroke_v2 is bound to that theme color and follows it if the theme changes.
THEME_CLASS = {"accent1": "Accent1", "accent2": "Accent2", "accent3": "Accent3", "accent4": "Accent4",
               "accent5": "Accent5", "accent6": "Accent6", "tx1": "Text1", "bg1": "Background1",
               "tx2": "Text2", "bg2": "Background2"}
NS_SVG_DOC = "http://www.w3.org/2000/svg"
DRAWN = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text"}
INHERITED = {"fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
             "stroke-dasharray", "stroke-opacity", "fill-opacity", "fill-rule", "clip-rule"}
KEPT = INHERITED | {"opacity"}

def _css_rules(root):
    """(selector, declarations) for the simple .class and tag selectors that icon exports use."""
    rules = []
    for st in root.iter("{%s}style" % NS_SVG_DOC, "style"):
        text = re.sub(r"/\*.*?\*/", "", st.text or "", flags=re.S)
        for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", text):
            decls = {k.strip().lower(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}
            rules += [(s.strip(), decls) for s in selectors.split(",")]
    return rules

def _declared(el, name, rules):
    """This element's own paint and stroke settings: attributes < tag rules < class rules < style attribute."""
    out = {k: el.get(k) for k in KEPT if el.get(k) is not None}
    classes = (el.get("class") or "").split()
    for sel, decls in [r for r in rules if r[0] == name] + [r for r in rules if r[0].startswith(".") and r[0][1:] in classes]:
        out.update({k: v for k, v in decls.items() if k in KEPT})
    for d in (el.get("style") or "").split(";"):
        if ":" in d:
            k, v = d.split(":", 1)
            if k.strip().lower() in KEPT:
                out[k.strip().lower()] = v.strip()
    return out

def _drawn(el, rules, inherited=None):
    """(element, resolved paint and stroke settings) for every drawn shape, CSS and inheritance applied."""
    if not isinstance(el.tag, str):
        return
    name = etree.QName(el).localname
    props = {**{k: v for k, v in (inherited or {}).items() if k in INHERITED}, **_declared(el, name, rules)}
    if name in DRAWN:
        yield el, props
    for child in el:
        yield from _drawn(child, rules, props)

def _painted(value):
    return value.strip().lower() not in ("none", "transparent")

def recolor_svg(svg_bytes, hex_color, theme_slot=None):
    """One color for every visible fill and stroke, whether set by attribute, style, CSS class or inheritance.
    Unpainted parts stay unpainted. With a theme slot, shapes are bound to it with PowerPoint's theme classes."""
    c = "#" + hex_color.lstrip("#").upper()
    root = etree.fromstring(svg_bytes, etree.XMLParser(resolve_entities=False, no_network=True))
    theme = THEME_CLASS.get(theme_slot)
    for el, props in list(_drawn(root, _css_rules(root))):
        fill = _painted(props.get("fill", "black"))      # SVG default fill: black
        stroke = _painted(props.get("stroke", "none"))
        for k, v in props.items():  # inline what the CSS said (stroke width, caps...) before CSS is dropped
            if k not in ("fill", "stroke"):
                el.set(k, v)
        el.set("fill", c if fill else "none")
        el.set("stroke", c if stroke else "none")
        classes = [f"MsftOfcThm_{theme}_{kind}_v2" for kind, on in (("Fill", fill), ("Stroke", stroke)) if theme and on]
        if classes:
            el.set("class", " ".join(classes))
        else:
            el.attrib.pop("class", None)
        el.attrib.pop("style", None)
    for st in list(root.iter("{%s}style" % NS_SVG_DOC, "style")):
        st.getparent().remove(st)
    if theme:
        style = etree.Element("{%s}style" % NS_SVG_DOC)
        style.text = f".MsftOfcThm_{theme}_Fill_v2{{fill:{c};}}.MsftOfcThm_{theme}_Stroke_v2{{stroke:{c};}}"
        root.insert(0, style)
    return etree.tostring(root).replace(b"currentColor", c.encode())

NAMED = {"black": "000000", "white": "FFFFFF"}

def svg_paint(svg_bytes):
    """Counter of the colors (RRGGBB) that paint visible fills and strokes, CSS and inheritance resolved."""
    from collections import Counter
    root = etree.fromstring(svg_bytes, etree.XMLParser(resolve_entities=False, no_network=True))
    counts = Counter()
    for _, props in _drawn(root, _css_rules(root)):
        for key, default in (("fill", "black"), ("stroke", "none")):
            v = props.get(key, default).strip().lower()
            v = NAMED.get(v, v.lstrip("#") if v.startswith("#") else "")
            if len(v) == 3:
                v = "".join(ch * 2 for ch in v)
            if re.fullmatch(r"[0-9a-f]{6}", v):
                counts[v.upper()] += 1
    return counts

def svg_aspect(svg_bytes):
    """(width, height) from the viewBox, else the width/height attributes, else square."""
    root = etree.fromstring(svg_bytes, etree.XMLParser(resolve_entities=False, no_network=True))
    vb = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    try:
        w, h = (float(vb[2]), float(vb[3])) if len(vb) == 4 else \
               (float(re.sub(r"[^\d.]", "", root.get("width"))), float(re.sub(r"[^\d.]", "", root.get("height"))))
        return (w, h) if w > 0 and h > 0 else (1.0, 1.0)
    except (TypeError, ValueError):
        return 1.0, 1.0

def stroke_pt(svg_bytes, size_in):
    """Rendered stroke weight in points at a given icon size (for the weight rule)."""
    root = etree.fromstring(svg_bytes)
    vb = root.get("viewBox")
    vw = float(vb.split()[2]) if vb else float(re.sub(r"[^\d.]", "", root.get("width", "24")))
    widths = []
    for e in root.iter():
        if not isinstance(e.tag, str):
            continue
        v = e.get("stroke-width") or (re.search(r"stroke-width\s*:\s*([\d.]+)", e.get("style", "")) or [None, None])[1]
        if v:
            widths.append(float(re.sub(r"[^\d.]", "", v)))
    sw = max(widths) if widths else None
    return round(sw * size_in * 72 / vw, 2) if sw else None

def svg_to_png(svg_bytes, px=512):
    """Transparent PNG fallback with the icon's own proportions, longest side px.
    resvg is a self-contained pip wheel (no system DLLs)."""
    import resvg_py
    return bytes(resvg_py.svg_to_bytes(svg_string=svg_bytes.decode("utf-8"), width=px, height=px,
                                       skip_system_fonts=True))

def insert_icon(prs, slide_idx, svg_bytes, left_in, top_in, size_in, hex_color,
                flip_h=False, name="Icon", theme_slot=None):
    """Insert a recolored SVG (vector, with a PNG fallback) on slide slide_idx (1-based).
    The icon's longer side is size_in; it keeps its proportions and is centered in the size_in square."""
    svg = recolor_svg(svg_bytes, hex_color, theme_slot)
    png = svg_to_png(svg)
    vw, vh = svg_aspect(svg)
    w, h = (size_in, size_in * vh / vw) if vw >= vh else (size_in * vw / vh, size_in)
    slide = prs.slides[slide_idx - 1]
    pic = slide.shapes.add_picture(io.BytesIO(png), Inches(left_in + (size_in - w) / 2),
                                   Inches(top_in + (size_in - h) / 2), Inches(w), Inches(h))
    pic.name = name
    # add the SVG part and point the picture's blip at it
    n = len([p for p in prs.part.package.iter_parts() if str(p.partname).endswith(".svg")]) + 1
    svg_part = Part(PackURI(f"/ppt/media/icon_svg{n}.svg"), "image/svg+xml", prs.part.package, svg)
    rid = slide.part.relate_to(svg_part, RT_IMAGE)
    blip = pic._element.find(".//{%s}blip" % NS_A)
    ext_lst = etree.SubElement(blip, "{%s}extLst" % NS_A)
    ext = etree.SubElement(ext_lst, "{%s}ext" % NS_A, uri=SVG_EXT)
    sb = etree.SubElement(ext, "{%s}svgBlip" % NS_SVG, nsmap={"asvg": NS_SVG})
    sb.set("{%s}embed" % NS_R, rid)
    if flip_h:
        pic._element.spPr.find("{%s}xfrm" % NS_A).set("flipH", "1")
    pic._element.nvPicPr.cNvPr.set("descr", name)  # alt text
    return pic

def copy_native_icon(src_path, src_slide, shape_name, dst_prs, dst_slide, left_in, top_in, size_in,
                     theme_color="accent1", flip_h=False, name="Icon"):
    """Copy a native (freeform/group) icon from a library .pptx, resize, recolor to a theme color."""
    src = Presentation(src_path)
    el = next(sh._element for sh in src.slides[src_slide - 1].shapes if sh.name == shape_name)
    if el.xpath(".//@r:embed | .//@r:link"):
        raise ValueError("icon contains embedded media; insert it as SVG instead")
    new = copy.deepcopy(el)
    tree = dst_prs.slides[dst_slide - 1].shapes._spTree
    next_id = max(int(i) for i in tree.xpath("//p:cNvPr/@id")) + 1
    for c in new.xpath(".//p:cNvPr"):
        c.set("id", str(next_id)); next_id += 1
    new.xpath("./*[1]/p:cNvPr")[0].set("name", name)
    new.xpath("./*[1]/p:cNvPr")[0].set("descr", name)
    xfrm = new.xpath("./p:grpSpPr/a:xfrm | ./p:spPr/a:xfrm")[0]
    off, ext = xfrm.find("{%s}off" % NS_A), xfrm.find("{%s}ext" % NS_A)
    w, h = int(ext.get("cx")), int(ext.get("cy"))
    s = Inches(size_in) / max(w, h)          # fit the longer side to the tier, keep proportions
    nw, nh = int(w * s), int(h * s)
    off.set("x", str(int(Inches(left_in) + (Inches(size_in) - nw) / 2)))
    off.set("y", str(int(Inches(top_in) + (Inches(size_in) - nh) / 2)))
    ext.set("cx", str(nw)); ext.set("cy", str(nh))
    if flip_h:
        xfrm.set("flipH", "1")
    for clr in new.xpath(".//a:solidFill/a:srgbClr | .//a:solidFill/a:schemeClr"):
        parent = clr.getparent(); parent.remove(clr)
        etree.SubElement(parent, "{%s}schemeClr" % NS_A, val=theme_color)
    tree.append(new)
    return new
