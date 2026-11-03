"""HTTP API and test UI. Run from the project root: uvicorn app.main:app --reload, then open http://127.0.0.1:8000"""
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config, jobs, library_edit
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
    return jobs.library_index(library)


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
    return Response(jobs.icon_svg(library.svg_bytes(icon), color), media_type="image/svg+xml")


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
        result = jobs.run(job_dir, deck.filename, library, mode, slide_list)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(400, str(e))
    return {"job": job, **result}


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
    try:
        result = jobs.apply(_job_dir(job), library, req.picks, req.choices)
    except jobs.RunError as e:
        raise HTTPException(e.status, str(e))
    return {**result, "download": f"jobs/{job}/download"}


@app.post("/jobs/{job}/uploads")
async def upload_icon(job: str, file: UploadFile = File(...)):
    """An icon a designer brings for a placeholder in this deck: an SVG, or a PNG/JPG/WebP/GIF/BMP traced into
    one, with the same checks as the library. Kept with the run, not added to the library."""
    try:
        return jobs.save_upload(_job_dir(job), await file.read(), file.filename)
    except jobs.RunError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/jobs/{job}/uploads/{digest}.svg")
def uploaded_svg(job: str, digest: str, color: str = ""):
    path = jobs.upload_path(_job_dir(job), f"upload:{digest}")
    if path is None or not path.exists():
        raise HTTPException(404, "no such upload")
    return Response(jobs.icon_svg(path.read_bytes(), color), media_type="image/svg+xml")


@app.get("/jobs/{job}/slides/{n}.png")
def slide_picture(job: str, n: int):
    """A picture of a slide with a flagged PL placeholder (jobs.slide_picture), for the review screen."""
    try:
        path = jobs.slide_picture(_job_dir(job), n)
    except jobs.RunError as e:
        raise HTTPException(e.status, e.detail)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/jobs/{job}/download")
def download(job: str):
    out = jobs.output(_job_dir(job))
    if out is None:
        raise HTTPException(404, "no output for this job")
    return FileResponse(out, filename=out.name)

# Claude (chat and the PowerPoint add-in) shows this same page, one run at a time, through the connector
# (connector.py), which has a secret address and job cleanup. This test UI has neither: keep it on 127.0.0.1.
