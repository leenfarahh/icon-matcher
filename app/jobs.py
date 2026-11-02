"""Saved runs: one folder per run under WORK_DIR holding the deck, its report and the icons designers uploaded for it.
Shared by the test UI (main.py) and the Claude connector (connector.py), which serve the same page (static/index.html),
so both run decks, preview icons and add chosen icons the same way."""
import hashlib
import json
import re
import shutil
import threading
from dataclasses import asdict
from pathlib import Path

from . import config, library_edit, pipeline, render, report
from .icon_tools import recolor_svg
from .library import Library

SAVED_REPORT = "report.json"
UPLOADS = "uploads"
PREVIEWS = "previews"
UPLOAD_ID = re.compile(r"upload:([0-9a-f]{12})")   # library ids are lowercase words and hyphens: never a colon


class RunError(ValueError):
    """A request the run can't take, with the reason in words a designer understands (or a list of them, from an
    icon's checks). status: the HTTP status."""

    def __init__(self, message: str | list[str], status: int = 400):
        super().__init__(message if isinstance(message, str) else "; ".join(message))
        self.detail, self.status = message, status


def run(job_dir: Path, deck_file: str, library: Library, mode: str, slides: list[int] | None) -> dict:
    """Run the deck in job_dir and save the report with it, so chosen suggestions can be inserted later without
    running the deck (or Claude) again. Returns the report (report.to_dict)."""
    result = report.to_dict(pipeline.run(job_dir / deck_file, library, mode, slides))
    (job_dir / SAVED_REPORT).write_text(json.dumps({"deck_file": deck_file, **result}), encoding="utf-8")
    return result


def saved(job_dir: Path) -> dict:
    path = job_dir / SAVED_REPORT
    if not path.exists():
        raise RunError("this run has no saved suggestions: run the deck again", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def upload_path(job_dir: Path, icon_id: str) -> Path | None:
    """The file of an icon uploaded for this run, or None when icon_id names a library icon."""
    m = UPLOAD_ID.fullmatch(icon_id)
    return job_dir / UPLOADS / f"{m.group(1)}.svg" if m else None


def output(job_dir: Path) -> Path | None:
    """The run's latest copy with icons, if there is one."""
    return next(iter(job_dir.glob("*_icons.pptx")), None)


def apply(job_dir: Path, library: Library, picks: list[int], choices: dict[int, str]) -> dict:
    """Insert the chosen picks, and the icons chosen for flagged PL placeholders (flag position -> a library id,
    or "upload:<id>" for one uploaded to this run), into a copy of the deck. The copy holds exactly these icons."""
    rep = saved(job_dir)
    chosen = sorted(set(picks))
    if not chosen and not choices:
        raise RunError("choose at least one icon")
    if any(not 0 <= n < len(rep["picks"]) for n in chosen):
        raise RunError("unknown suggestion: run the deck again")
    flags = rep.get("flags", [])
    if any(not 0 <= n < len(flags) or not flags[n].get("spot") for n in choices):
        raise RunError("that placeholder can't take a chosen icon: run the deck again")
    style = rep["settings"]["style"]   # an approved icon in the deck's style (options or a search), or an upload
    for icon_id in choices.values():
        upload = upload_path(job_dir, icon_id)
        if upload is None and (not (icon := library.get(icon_id)) or icon.style != style):
            raise RunError(f"choose approved {style} icons from the library, or upload one")
        if upload is not None and not upload.exists():
            raise RunError("an uploaded icon is missing: upload it again")
    placements = pipeline.placements_from_report(rep, chosen, choices)
    for p in placements:
        if (upload := upload_path(job_dir, p["icon_id"])) is not None:
            p["svg_file"] = str(upload)
    out, skipped = pipeline.insert(job_dir / rep["deck_file"], library, placements, rep["settings"])
    shutil.rmtree(job_dir / PREVIEWS, ignore_errors=True)   # slide pictures now show the new copy
    return {"added": len(placements) - len(skipped), "skipped": skipped, "output_file": out.name}


def library_index(library: Library) -> dict:
    """The page's Library tab: every icon of any status (each carries its status); the concept map covers approved
    icons only."""
    return {"icons": [asdict(i) for i in library.all.values()], "concept_map": library.concept_map,
            "warnings": library.warnings, "indexed_from_filenames": library.indexed_from_filenames,
            "llm": config.USE_LLM}


def icon_svg(svg: bytes, color: str = "") -> bytes:
    """An icon's SVG; with color (a hex such as FD5108), recolored as it would be in a deck (for previews)."""
    return recolor_svg(svg, color) if re.fullmatch(r"[0-9A-Fa-f]{6}", color) else svg


def save_upload(job_dir: Path, data: bytes, filename: str) -> dict:
    """An icon a designer brings for a placeholder in this deck: an SVG, or a PNG/JPG/WebP/GIF/BMP traced into one,
    with the same checks as the library. Kept with the run, not added to the library."""
    svg, errors, warnings, source_format = library_edit.prepare_upload(data)
    if errors:
        raise RunError(errors)
    digest = hashlib.sha1(svg).hexdigest()[:12]
    (job_dir / UPLOADS).mkdir(exist_ok=True)
    (job_dir / UPLOADS / f"{digest}.svg").write_bytes(svg)
    return {"id": f"upload:{digest}", "name": filename, "warnings": warnings, "source_format": source_format}


_rendering = threading.Lock()


def slide_picture(job_dir: Path, n: int) -> Path:
    """A picture of a slide with a flagged PL placeholder, from the run's latest deck (the copy with icons when
    there is one), for the review screen. The first request renders all of them in one PowerPoint session; later
    ones come from the run's cache, which adding icons clears. Without PowerPoint (on Render) there is none, and the
    page shows the placeholders on a blank slide."""
    path = job_dir / PREVIEWS / f"slide-{n}.png"
    if path.exists():
        return path
    rep = saved(job_dir)
    wanted = sorted({f["item"]["slide"] for f in rep.get("flags", []) if f.get("spot")})
    if n not in wanted:
        raise RunError("no flagged placeholder on that slide", 404)
    if not render.available():
        raise RunError("slide pictures need PowerPoint on the machine running the tool", 404)
    w, h = rep.get("slide_size") or (13.333, 7.5)
    deck = output(job_dir) or job_dir / rep["deck_file"]
    with _rendering:   # parallel requests: the first renders every slide, the rest find theirs done
        if not path.exists():
            try:
                render.slides(deck, wanted, job_dir / PREVIEWS, w / h)
            except RuntimeError as e:
                raise RunError(str(e), 503)
    return path
