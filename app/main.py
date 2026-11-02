"""HTTP API and test UI. Run from the project root: uvicorn app.main:app --reload, then open http://127.0.0.1:8000"""
import hashlib
import json
import re
import shutil
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config, library_edit, pipeline, render, report
from .icon_tools import recolor_svg
from .library import Library

app = FastAPI(title="Icon Placer")
library = Library(config.LIBRARY_DIR)
STATIC = Path(__file__).parent / "static"


@app.get("/")
def ui():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"library": str(library.root), "found": library.root.exists(),
            "icons": len(library.icons), "concepts": len(library.concept_map), "llm": config.USE_LLM}


@app.get("/library")
def library_index():
    """Every icon of any status (each carries its status); the concept map covers approved icons only."""
    return {"icons": [asdict(i) for i in library.all.values()], "concept_map": library.concept_map,
            "warnings": library.warnings, "indexed_from_filenames": library.indexed_from_filenames,
            "llm": config.USE_LLM}


@app.post("/library/preview")
async def preview_library_icon(file: UploadFile = File(...)):
    """What would be saved for this upload: the SVG as-is, or the vector traced from a PNG/JPG. Writes nothing."""
    svg, errors, warnings, source_format = library_edit.prepare_upload(await file.read())
    return {"svg": svg.decode("utf-8", "replace"), "source_format": source_format,
            "errors": errors, "warnings": warnings}


@app.post("/library/icons")
async def add_library_icon(request: Request):
    """Add one icon: the upload ("file": SVG, or PNG/JPG/WebP/GIF/BMP to trace) plus its icons.json fields
    as form fields. Reloads the library."""
    global library
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise HTTPException(400, ["choose an icon file (SVG, or a PNG or JPG to convert)"])
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    try:
        record, warnings = library_edit.add_icon(library.root, fields, await upload.read())
    except library_edit.InvalidIcon as e:
        raise HTTPException(400, e.errors)
    library = Library(config.LIBRARY_DIR)
    return {"icon": record, "warnings": warnings}


@app.patch("/library/icons/{icon_id}")
async def edit_library_icon(icon_id: str, request: Request):
    """Change an icon's fields (form fields; any left out keep their value). The id and file stay as they are."""
    global library
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str) and k not in ("id", "file")}
    try:
        record, warnings = library_edit.update_icon(library.root, icon_id, fields)
    except KeyError:
        raise HTTPException(404, [f"no icon with id '{icon_id}'"])
    except library_edit.InvalidIcon as e:
        raise HTTPException(400, e.errors)
    library = Library(config.LIBRARY_DIR)
    return {"icon": record, "warnings": warnings}


@app.get("/icons/{icon_id}.svg")
def icon_svg(icon_id: str, color: str = ""):
    """An icon's SVG; with color (a hex such as FD5108), recolored as it would be in a deck (for previews)."""
    icon = library.all.get(icon_id)
    if not icon:
        raise HTTPException(404, "unknown icon")
    return _svg(library.svg_bytes(icon), color)


def _svg(svg: bytes, color: str = "") -> Response:
    if re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        svg = recolor_svg(svg, color)
    return Response(svg, media_type="image/svg+xml")


@app.post("/jobs")
async def create_job(deck: UploadFile = File(...), mode: str = Form("replace"), slides: str = Form("")):
    if not deck.filename.lower().endswith(".pptx"):
        raise HTTPException(400, "upload a .pptx")
    job = uuid.uuid4().hex[:12]
    job_dir = config.WORK_DIR / job
    job_dir.mkdir(parents=True)
    src = job_dir / deck.filename
    with src.open("wb") as f:
        shutil.copyfileobj(deck.file, f)
    slide_list = [int(s) for s in slides.split(",") if s.strip()] or None
    try:
        r = pipeline.run(src, library, mode, slide_list)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(400, str(e))
    result = report.to_dict(r)
    # kept so chosen suggestions can be inserted later without running the deck (or Claude) again
    (job_dir / SAVED_REPORT).write_text(json.dumps({"deck_file": deck.filename, **result}), encoding="utf-8")
    return {"job": job, **result}


SAVED_REPORT = "report.json"
JOB_ID = re.compile(r"[0-9a-f]{12}")


def _job_dir(job: str) -> Path:
    if not JOB_ID.fullmatch(job) or not (config.WORK_DIR / job).is_dir():
        raise HTTPException(404, "no such run: run the deck again")
    return config.WORK_DIR / job


class ApplyRequest(BaseModel):
    picks: list[int] = []          # positions in the run's picks list
    choices: dict[int, str] = {}   # flag position -> the icon a designer chose for that PL placeholder (a library
                                   # id, or "upload:<id>" for one uploaded to this run)


@app.post("/jobs/{job}/apply")
def apply_suggestions(job: str, req: ApplyRequest):
    """Insert the chosen picks, and the icons chosen for flagged PL placeholders, into a copy of the deck. The
    copy holds exactly these icons."""
    job_dir = _job_dir(job)
    saved_path = job_dir / SAVED_REPORT
    if not saved_path.exists():
        raise HTTPException(404, "this run has no saved suggestions: run the deck again")
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    chosen = sorted(set(req.picks))
    if not chosen and not req.choices:
        raise HTTPException(400, "choose at least one icon")
    if any(not 0 <= n < len(saved["picks"]) for n in chosen):
        raise HTTPException(400, "unknown suggestion: run the deck again")
    flags = saved.get("flags", [])
    if any(not 0 <= n < len(flags) or not flags[n].get("spot") for n in req.choices):
        raise HTTPException(400, "that placeholder can't take a chosen icon: run the deck again")
    style = saved["settings"]["style"]   # an approved icon in the deck's style (options or a search), or an upload
    for icon_id in req.choices.values():
        upload = _upload_path(job_dir, icon_id)
        if upload is None and (not (icon := library.get(icon_id)) or icon.style != style):
            raise HTTPException(400, f"choose approved {style} icons from the library, or upload one")
        if upload is not None and not upload.exists():
            raise HTTPException(400, "an uploaded icon is missing: upload it again")
    placements = pipeline.placements_from_report(saved, chosen, req.choices)
    for p in placements:
        if (upload := _upload_path(job_dir, p["icon_id"])) is not None:
            p["svg_file"] = str(upload)
    out, skipped = pipeline.insert(job_dir / saved["deck_file"], library, placements, saved["settings"])
    shutil.rmtree(job_dir / PREVIEWS, ignore_errors=True)   # slide pictures now show the new copy
    return {"added": len(placements) - len(skipped), "skipped": skipped, "output_file": out.name,
            "download": f"jobs/{job}/download"}


UPLOADS = "uploads"
UPLOAD_ID = re.compile(r"upload:([0-9a-f]{12})")   # library ids are lowercase words and hyphens: never a colon


def _upload_path(job_dir: Path, icon_id: str) -> Path | None:
    """The file of an icon uploaded for this run, or None when icon_id names a library icon."""
    m = UPLOAD_ID.fullmatch(icon_id)
    return job_dir / UPLOADS / f"{m.group(1)}.svg" if m else None


@app.post("/jobs/{job}/uploads")
async def upload_icon(job: str, file: UploadFile = File(...)):
    """An icon a designer brings for a placeholder in this deck: an SVG, or a PNG/JPG/WebP/GIF/BMP traced into
    one, with the same checks as the library. Kept with the run, not added to the library."""
    job_dir = _job_dir(job)
    svg, errors, warnings, source_format = library_edit.prepare_upload(await file.read())
    if errors:
        raise HTTPException(400, errors)
    digest = hashlib.sha1(svg).hexdigest()[:12]
    (job_dir / UPLOADS).mkdir(exist_ok=True)
    (job_dir / UPLOADS / f"{digest}.svg").write_bytes(svg)
    return {"id": f"upload:{digest}", "name": file.filename, "warnings": warnings, "source_format": source_format}


@app.get("/jobs/{job}/uploads/{digest}.svg")
def uploaded_svg(job: str, digest: str, color: str = ""):
    path = _upload_path(_job_dir(job), f"upload:{digest}")
    if path is None or not path.exists():
        raise HTTPException(404, "no such upload")
    return _svg(path.read_bytes(), color)


PREVIEWS = "previews"
_rendering = threading.Lock()


@app.get("/jobs/{job}/slides/{n}.png")
def slide_picture(job: str, n: int):
    """A picture of a slide with a flagged PL placeholder, from the run's latest deck (the copy with icons when
    there is one), for the review screen. The first request renders all of them in one PowerPoint session;
    later ones come from the job's cache, which adding icons clears."""
    job_dir = _job_dir(job)
    path = job_dir / PREVIEWS / f"slide-{n}.png"
    if not path.exists():
        saved = json.loads((job_dir / SAVED_REPORT).read_text(encoding="utf-8"))
        wanted = sorted({f["item"]["slide"] for f in saved.get("flags", []) if f.get("spot")})
        if n not in wanted:
            raise HTTPException(404, "no flagged placeholder on that slide")
        if not render.available():
            raise HTTPException(404, "slide pictures need PowerPoint on the machine running the tool")
        w, h = saved.get("slide_size") or (13.333, 7.5)
        deck = next(iter(job_dir.glob("*_icons.pptx")), job_dir / saved["deck_file"])
        with _rendering:   # parallel requests: the first renders every slide, the rest find theirs done
            if not path.exists():
                try:
                    render.slides(deck, wanted, job_dir / PREVIEWS, w / h)
                except RuntimeError as e:
                    raise HTTPException(503, str(e))
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/jobs/{job}/download")
def download(job: str):
    files = list(_job_dir(job).glob("*_icons.pptx"))
    if not files:
        raise HTTPException(404, "no output for this job")
    return FileResponse(files[0], filename=files[0].name)

# TODO: auth (Microsoft 365 SSO), job cleanup, a small upload UI, and an Office add-in
# front end that posts the open deck here and inserts the returned SVGs with Office.js.
