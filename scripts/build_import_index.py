"""Merge tagger JSON files into icons_import/icons.json (staging library, same schema as icons/icons.json).

    python -I -X utf8 scripts/build_import_index.py tags1.json tags2.json ...
Rejected fragments move to icons_import/rejected/. Blocked icons are written as status "deprecated".
"""
import json, shutil, sys
from pathlib import Path

ROOT = Path("icons_import")
SRC = {"k": "1000 icon 4.ai", "o": "icons outline [Recovered] 6.ai"}
BLOCK = {"thumbs", "thumb-up", "ok-hand", "pointing", "handshake", "hand", "pray", "praying", "church", "mosque",
         "temple", "cross", "crescent", "star-of-david", "wine", "beer", "cocktail", "champagne", "toast",
         "alcohol", "pig", "piggy", "pork", "map-borders", "country-map", "region-map"}

# Sheet labels count kept icons only (clusters.json order), file names also count skipped fragments.
LABEL = {}
for pre in SRC:
    for c in json.load(open(ROOT / f"svg_{pre}" / "clusters.json")):
        LABEL.setdefault((pre, c["page"]), []).append(c["id"])

recs = {}
for f in sys.argv[1:]:
    for r in json.load(open(f, encoding="utf-8")):
        recs[(r["sheet"], r["n"])] = r          # later file wins on a duplicate
index, rejected, problems = [], [], []
(ROOT / "rejected").mkdir(exist_ok=True)
for (sheet, n), r in sorted(recs.items()):
    pre, page = sheet.split("-p")
    ids = LABEL[(pre, int(page))]
    if not 1 <= n <= len(ids):
        problems.append(f"{sheet} #{n}: no such icon on the sheet"); continue
    iid = ids[n - 1]
    svg = ROOT / f"svg_{pre}" / f"{iid}.svg"
    if not svg.exists():
        problems.append(f"{iid}: no svg"); continue
    if r.get("reject"):
        shutil.move(svg, ROOT / "rejected" / svg.name); rejected.append(iid); continue
    tags = [t.strip().lower() for t in r["tags"]]
    hit = sorted(b for b in BLOCK for v in [iid, *tags] if b in v)
    blocked = bool(r.get("blocked")) or bool(hit)
    if hit: problems.append(f"{iid}: block-list hit {hit} -> deprecated")
    index.append({
        "id": iid, "file": f"svg_{pre}/{iid}.svg", "status": "deprecated" if blocked else "sample",
        "style": "outline", "family": "detailed", "category": r["category"],
        "description": r["description"], "description_ar": r.get("description_ar", ""),
        "tags": tags, "tags_ar": r.get("tags_ar", []), "preferred_for": [], "avoid_for": [],
        "contexts": r.get("contexts", []), "metaphor_strength": r.get("metaphor_strength", "strong"),
        "pairs_with": [], "rtl_flip": bool(r.get("rtl_flip")), "source": f"{SRC[pre]}, sheet page {int(page)}", "license": ""})
done = {i["id"] for i in index} | set(rejected)
for p in ROOT.glob("svg_*/*.svg"):
    if p.stem not in done: problems.append(f"{p.stem}: svg with no record")
(ROOT / "icons.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(index), "indexed,", sum(i["status"] == "deprecated" for i in index), "blocked,", len(rejected), "rejected")
print("\n".join(problems[:40]), f"\n{len(problems)} problems")
