"""Split a PDF-compatible Illustrator (.ai) icon sheet into one SVG per icon.

Icons are found by grouping vector shapes whose bounding boxes touch or sit within --gap points.
Output (in --out): <prefix>-<page>-<n>.svg plus clusters.json (bbox, source page, flags for review).

    python -I -X utf8 scripts/extract_ai_icons.py "sheet.ai" --prefix outline --out icons_import/svg
"""
import argparse
import json
from pathlib import Path

import pymupdf

SIZE = 512


def cluster(rects, gap):
    """Union-find over rects that overlap once each is grown by gap/2."""
    n = len(rects)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    g = gap / 2
    order = sorted(range(n), key=lambda i: rects[i].x0)
    for a_pos, i in enumerate(order):
        ri = rects[i]
        for j in order[a_pos + 1:]:
            rj = rects[j]
            if rj.x0 - g > ri.x1 + g:
                break
            if rj.y0 - g <= ri.y1 + g and ri.y0 - g <= rj.y1 + g:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def hexcol(c):
    return "#%02x%02x%02x" % tuple(round(v * 255) for v in c)


def paint(c, mono):
    if c is None:
        return "none"
    if mono and sum(c) / len(c) < 0.5:
        return "currentColor"
    return hexcol(c)


def path_data(d, tx, ty, s):
    def pt(p):
        return f"{(p.x - tx) * s:.2f} {(p.y - ty) * s:.2f}"

    out, cur = [], None
    for it in d["items"]:
        k = it[0]
        if k == "re":
            r = it[1]
            # orientation 1 = the PDF "re" winding (bl, br, tr, tl on screen); keep it so holes cut out
            ring = (r.bl, r.br, r.tr, r.tl) if (it[2] if len(it) > 2 else 1) == 1 else (r.tl, r.tr, r.br, r.bl)
            out.append(f"M{pt(ring[0])}" + "".join(f"L{pt(q)}" for q in ring[1:]) + "Z")
            cur = None
            continue
        if k == "qu":
            q = it[1]
            out.append(f"M{pt(q.ul)}L{pt(q.ur)}L{pt(q.lr)}L{pt(q.ll)}Z")
            cur = None
            continue
        start = it[1]
        if cur is None or abs(cur.x - start.x) > 0.01 or abs(cur.y - start.y) > 0.01:
            out.append(f"M{pt(start)}")
        if k == "l":
            out.append(f"L{pt(it[2])}")
            cur = it[2]
        elif k == "c":
            out.append(f"C{pt(it[2])} {pt(it[3])} {pt(it[4])}")
            cur = it[4]
    if d.get("closePath"):
        out.append("Z")
    return "".join(out)


def to_svg(draws, box, mono):
    w, h = box.width, box.height
    side = max(w, h)
    s = (SIZE - 8) / side               # 4 px margin
    ox = box.x0 - (side - w) / 2 - 4 / s
    oy = box.y0 - (side - h) / 2 - 4 / s
    parts = []
    for d in draws:
        fill = paint(d.get("fill"), mono) if d.get("fill") is not None else "none"
        stroke = paint(d.get("color"), mono) if d.get("color") is not None else "none"
        if fill == "none" and stroke == "none":
            continue
        attrs = f'd="{path_data(d, ox, oy, s)}" fill="{fill}"'
        if fill != "none":      # even-odd: icon compound shapes cut their holes whatever the winding
            attrs += ' fill-rule="evenodd"'
        if stroke != "none":
            attrs += (f' stroke="{stroke}" stroke-width="{(d.get("width") or 1) * s:.2f}"'
                      f' stroke-linecap="round" stroke-linejoin="round"')
        parts.append(f"<path {attrs}/>")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
            f'viewBox="0 0 {SIZE} {SIZE}">{"".join(parts)}</svg>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ai")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--keep-color", action="store_true", help="keep original colors (default: dark -> currentColor)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(a.ai)
    meta, skipped = [], 0
    for pno, page in enumerate(doc, 1):
        draws = [d for d in page.get_drawings() if d["items"]]
        if not draws:
            continue
        rects = [pymupdf.Rect(d["rect"]) for d in draws]
        for n, idx in enumerate(sorted(cluster(rects, a.gap), key=lambda g: (round(min(rects[i].y0 for i in g) / 40), min(rects[i].x0 for i in g))), 1):
            box = pymupdf.Rect(rects[idx[0]])
            for i in idx:
                box |= rects[i]
            flags = []
            if max(box.width, box.height) < 12:
                flags.append("tiny")
            if len(idx) == 1 and max(box.width, box.height) < 25:
                flags.append("single-shape-small")
            name = f"{a.prefix}-p{pno:02d}-{n:03d}"
            if flags:   # stray dots, dashes and single small shapes: not icons, so no SVG
                skipped += 1
                continue
            (out / f"{name}.svg").write_text(
                to_svg([draws[i] for i in idx], box, not a.keep_color), encoding="utf-8")
            meta.append({"id": name, "page": pno, "bbox": [round(v, 1) for v in box], "shapes": len(idx), "flags": flags})
    (out / "clusters.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{len(meta)} icons kept, {skipped} stray fragments skipped -> {out}")


if __name__ == "__main__":
    main()
