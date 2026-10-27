"""Add icons to the library: validate a new record and its SVG, then append it to icons.json.
The same rules back the tests (validate_records), so the UI can't save what pytest would reject.
Raster uploads (PNG, JPG, ...) are traced to SVG first; the original is kept in originals/."""
import json
import os
import re
from pathlib import Path

from lxml import etree

from . import config, vectorize
from .library import INDEX_FILE

ORIGINALS_DIR = "originals"

FIELDS = ["id", "file", "status", "style", "category", "description", "description_ar", "tags", "tags_ar",
          "preferred_for", "avoid_for", "contexts", "metaphor_strength", "pairs_with", "rtl_flip", "source", "license"]
LIST_FIELDS = ["tags", "tags_ar", "preferred_for", "avoid_for", "contexts", "pairs_with"]
REQUIRED = ["id", "file", "status", "style", "category", "description", "tags", "metaphor_strength"]
STATUSES = {"approved", "sample", "deprecated"}
STRENGTHS = {"strong", "weak"}
ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
CATEGORY_RE = re.compile(r"[a-z][a-z0-9-]*")
ARABIC_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟـ]")  # diacritics and tatweel
COLOR_RE = re.compile(r"(?:fill|stroke|stop-color|color)\s*[:=]\s*[\"']?(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b")
MAX_SVG_BYTES = 1_000_000


class InvalidIcon(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _list(record: dict, key: str) -> list:
    v = record.get(key, [])
    return v if isinstance(v, list) else []


def validate_records(records: list[dict], root: Path, new_files=(), focus: str | None = None) -> list[str]:
    """Every rule icons.json must pass. Returns readable errors; empty means valid.
    `focus` is the icon being saved: a house-pick clash is reported against it, not the icon it clashes with."""
    errors, seen, owners = [], set(), {}
    ids = {r.get("id") for r in records}
    for r in records:
        rid = r.get("id") or "(no id)"
        # one message per field, so a save is blocked only by problems it introduces (see add_icon)
        errors += [f"{rid}: missing {f}" for f in REQUIRED if not r.get(f)]
        if r.get("id") and not ID_RE.fullmatch(r["id"]):
            errors.append(f"{rid}: id must be lowercase words joined by hyphens (e.g. growth-arrow)")
        if rid in seen:
            errors.append(f"{rid}: id is used twice")
        seen.add(rid)
        if r.get("category") and not CATEGORY_RE.fullmatch(r["category"]):
            errors.append(f"{rid}: category must be one lowercase word")
        if r.get("status") and r["status"] not in STATUSES:
            errors.append(f"{rid}: status must be approved, sample or deprecated")
        if r.get("metaphor_strength") and r["metaphor_strength"] not in STRENGTHS:
            errors.append(f"{rid}: metaphor_strength must be strong or weak")
        if not isinstance(r.get("rtl_flip", False), bool):
            errors.append(f"{rid}: rtl_flip must be true or false")
        errors += [f"{rid}: {f} must be a list" for f in LIST_FIELDS if not isinstance(r.get(f, []), list)]
        if r.get("file") and r["file"] not in new_files and not (root / r["file"]).is_file():
            errors.append(f"{rid}: {r['file']} is not in the library folder")
        unknown = [p for p in _list(r, "pairs_with") if p not in ids]
        if unknown:
            errors.append(f"{rid}: pairs_with has unknown ids ({', '.join(unknown)})")
        hits = sorted({b for b in config.REGIONAL_BLOCKLIST for v in [rid, *_list(r, "tags")] if b in str(v).lower()})
        if hits:
            errors.append(f"{rid}: id or tags hit the regional block list ({', '.join(hits)}); reword or don't add it")
    for r in sorted(records, key=lambda r: r.get("id") == focus):  # stable sort: the focus icon claims last
        rid = r.get("id") or "(no id)"
        for c in (str(c).lower() for c in _list(r, "preferred_for")):
            if c in owners:
                errors.append(f"{rid}: '{c}' is already the house pick for {owners[c]}")
            else:
                owners[c] = rid
    return errors


def check_svg(svg: bytes) -> tuple[list[str], list[str]]:
    """Errors block the upload (unsafe or unusable files). Warnings are saved but reported."""
    if not svg:
        return ["the SVG file is empty"], []
    if len(svg) > MAX_SVG_BYTES:
        return [f"the SVG is {len(svg) // 1024} KB; icons should be far under 1 MB"], []
    if re.search(rb"<!(DOCTYPE|ENTITY)", svg, re.I):
        return ["the SVG declares a DOCTYPE or entities; export it again as plain SVG"], []
    try:
        root = etree.fromstring(svg, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    except etree.XMLSyntaxError:
        return ["the file is not a valid SVG"], []
    if etree.QName(root).localname != "svg":
        return ["the file is not an SVG (no <svg> root element)"], []

    errors, warnings, names, css = [], [], set(), []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname
        names.add(name)
        css.append(el.get("style", ""))
        if name == "style":
            css.append(el.text or "")
        for attr, value in el.attrib.items():
            a = etree.QName(attr).localname.lower()
            if a.startswith("on"):
                errors.append(f"<{name}> has a script attribute ({a}); remove it")
            if a == "href" and name != "image" and not value.startswith("#"):
                errors.append(f"<{name}> links to an outside resource; remove the link")
    if names & {"script", "foreignObject"}:
        errors.append("the SVG contains scripts or embedded HTML; remove them")
    if "image" in names:
        errors.append("the SVG contains a raster image; icons must be pure vector")
    if re.search(r"@import|url\(\s*[\"']?(?!#)", " ".join(css)):
        errors.append("the SVG's styles load outside resources (@import or url(...)); remove them")

    if "text" in names:
        warnings.append("the SVG has live text; convert text to outlines so it renders the same everywhere")
    if names & {"linearGradient", "radialGradient", "pattern"}:
        warnings.append("the SVG uses gradients or patterns; icons should be one flat color")
    colors = {_hex(c) for c in COLOR_RE.findall(svg.decode("utf-8", "ignore"))}
    if len(colors) > 1:
        warnings.append(f"the SVG uses {len(colors)} colors; icons are recolored to one brand color, "
                        "so detail drawn in a second color will be lost")
    if not root.get("viewBox"):
        warnings.append("the SVG has no viewBox; export it with one so it scales cleanly")
    return list(dict.fromkeys(errors)), warnings


def prepare_upload(data: bytes) -> tuple[bytes, list[str], list[str], str | None]:
    """SVGs are checked as they are; raster images are traced to SVG first.
    Returns (svg, errors, warnings, source format or None for an SVG upload)."""
    if vectorize.is_svg(data):
        errors, warnings = check_svg(data)
        return data, errors, warnings, None
    try:
        svg, fmt, warnings = vectorize.raster_to_svg(data)
    except ValueError as e:
        return b"", [str(e)], [], None
    errors, svg_warnings = check_svg(svg)
    return svg, errors, warnings + svg_warnings, fmt


def _hex(c: str) -> str:
    c = c.lower().lstrip("#")
    return "#" + ("".join(ch * 2 for ch in c) if len(c) == 3 else c)


def build_record(fields: dict) -> dict:
    """Form fields -> an icons.json record. Lists arrive as text separated by ; , or the Arabic comma."""
    def text(k):
        return " ".join(str(fields.get(k) or "").split())

    def items(k, arabic=False):
        out = []
        for v in re.split(r"[;,،\n]+", str(fields.get(k) or "")):
            v = " ".join(v.split())
            v = ARABIC_MARKS.sub("", v) if arabic else v.lower()
            if v and v not in out:
                out.append(v)
        return out

    icon_id = text("id").lower()
    preferred = items("preferred_for")
    tags = items("tags")
    tags += [p for p in preferred if p not in tags]  # every house-pick concept is also a tag
    return {
        "id": icon_id, "file": f"{icon_id}.svg" if icon_id else "",
        "status": text("status").lower() or "approved", "style": text("style").lower() or config.DEFAULT_STYLE,
        "category": text("category").lower(), "description": text("description"),
        "description_ar": text("description_ar"), "tags": tags, "tags_ar": items("tags_ar", arabic=True),
        "preferred_for": preferred, "avoid_for": items("avoid_for"), "contexts": items("contexts"),
        "metaphor_strength": text("metaphor_strength").lower() or "strong", "pairs_with": items("pairs_with"),
        "rtl_flip": text("rtl_flip").lower() in {"true", "on", "yes", "1"},
        "source": text("source"), "license": text("license"),
    }


def dump_records(records: list[dict]) -> str:
    """The icons.json layout: one block per icon, one field per line, lists on one line."""
    blocks = []
    for r in records:
        keys = [k for k in FIELDS if k in r] + [k for k in r if k not in FIELDS]
        body = ",\n".join(f"    {json.dumps(k)}: {json.dumps(r[k], ensure_ascii=False)}" for k in keys)
        blocks.append("  {\n" + body + "\n  }")
    return "[\n" + ",\n".join(blocks) + "\n]\n"


def add_icon(root: Path, fields: dict, data: bytes) -> tuple[dict, list[str]]:
    """Validate, save <id>.svg next to icons.json and append the record. Nothing is written on error.
    `data` is an SVG or a raster image (traced to SVG). Only problems the new icon introduces block it;
    existing issues are left to pytest."""
    root = Path(root)
    index = root / INDEX_FILE
    records = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    record = build_record(fields)
    svg, svg_errors, warnings, source_format = prepare_upload(data)
    before = set(validate_records(records, root))
    errors = svg_errors + [e for e in validate_records(records + [record], root, new_files={record["file"]})
                           if e not in before]
    if record["file"] and (root / record["file"]).exists():
        errors.append(f"{record['file']} already exists in the library folder")
    if errors:
        raise InvalidIcon(errors)
    warnings += _license_warning(record)

    (root / record["file"]).write_bytes(svg)
    if source_format:
        original = f"{ORIGINALS_DIR}/{record['id']}.{vectorize.RASTER_FORMATS[source_format]}"
        (root / ORIGINALS_DIR).mkdir(exist_ok=True)
        (root / original).write_bytes(data)
        warnings.insert(0, f"traced from {source_format} to SVG; the original is kept as {original}")
    _write_index(index, records + [record])
    return record, warnings


def update_icon(root: Path, icon_id: str, fields: dict) -> tuple[dict, list[str]]:
    """Change an existing icon's fields. Fields left out keep their value. The id and file never change:
    decks and other icons refer to them. Raises KeyError for an unknown id, InvalidIcon on a rule break;
    nothing is written on error. To retire an icon, set its status to deprecated."""
    root = Path(root)
    index = root / INDEX_FILE
    records = json.loads(index.read_text(encoding="utf-8"))
    pos = next((n for n, r in enumerate(records) if r.get("id") == icon_id), None)
    if pos is None:
        raise KeyError(icon_id)
    old = records[pos]
    current = {k: _as_field(v) for k, v in old.items()}
    updated = {**old, **build_record({**current, **fields, "id": icon_id}), "file": old.get("file", "")}
    candidate = records[:pos] + [updated] + records[pos + 1:]
    before = set(validate_records(records, root, focus=icon_id))
    errors = [e for e in validate_records(candidate, root, focus=icon_id) if e not in before]
    if errors:
        raise InvalidIcon(errors)
    _write_index(index, candidate)
    return updated, _license_warning(updated)


def _as_field(value) -> str:
    """A stored value as the form would send it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "; ".join(map(str, value))
    return "" if value is None else str(value)


def _license_warning(record: dict) -> list[str]:
    if record.get("status") == "approved" and not record.get("license"):
        return ["license is empty: fill it in before the icon goes into client decks"]
    return []


def _write_index(index: Path, records: list[dict]):
    """Write via a temp file and swap, so icons.json is never left half-written."""
    tmp = index.with_name(INDEX_FILE + ".tmp")
    tmp.write_text(dump_records(records), encoding="utf-8")
    os.replace(tmp, index)
