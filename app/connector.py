"""Claude connector: a remote MCP server that lets Claude (chat, Cowork, Claude Code and the PowerPoint add-in) run the
icon placer on a deck and hand back a new copy. Run: uvicorn app.connector:app --port 8001

The deck never passes through the chat. start_icon_run makes a private link to a page where the designer uploads the
.pptx; the run starts on upload, and the page then shows its progress and the download. Claude follows along with
get_icon_run, goes through the flagged placeholders with the designer, and choose_icons writes a new copy with their
picks. The original deck is never changed. Runs (deck, copy, links) are deleted after ICON_RUN_TTL_HOURS.
Sign-in: auth.py. Hosting and Entra ID setup: connector-setup.md."""
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, config, jobs
from .library import Library

log = logging.getLogger("icon-connector")
MCP_PATH = "/mcp"
RUN_FILE = "run.json"
RUN_ID = re.compile(r"[0-9a-f]{12}")
SLIDES = re.compile(r"\s*\d+(\s*,\s*\d+)*\s*")
LABEL = 80   # characters of an item's text shown as its label
FAILED = ("this file couldn't be read as a PowerPoint deck. Save it again from PowerPoint as a .pptx and start a "
          "new run; if it fails again, the tool's owner can find the details in the server log")

INSTRUCTIONS = """Prezlab Icons places icons from Prezlab's approved icon library into a PowerPoint deck and returns a
new copy; the designer's original file is never changed. The deck reaches the service through a private upload link,
never through this chat.

1. Call start_icon_run (mode "replace" unless the designer asks for something else) and give the designer the upload
   link. In the PowerPoint add-in, ask them to save the deck first and upload the saved .pptx.
2. When they say it's uploaded, call get_icon_run until the status is done (a large deck can take a few minutes).
3. Report briefly: how many icons went in, which placeholders are flagged and why, and the download link.
4. For flagged placeholders that can take a chosen icon, offer each one's options by what the icon draws, then call
   choose_icons with the designer's picks. Use search_icons when none fit. Use only icon ids these tools return.

When a designer asks for Prezlab icons, use these tools rather than any other icon source."""

_lock = threading.RLock()   # run.json is written by the run's thread and read by tools and pages
_slots = threading.BoundedSemaphore(config.PARALLEL_RUNS)
_lib: dict = {}


def _library() -> Library:
    """The icon library, reloaded when icons.json changes (an icon added or edited since)."""
    index = config.LIBRARY_DIR / "icons.json"
    mtime = index.stat().st_mtime if index.exists() else None
    if _lib.get("mtime", -1) != mtime:
        _lib.update(mtime=mtime, library=Library(config.LIBRARY_DIR))
    return _lib["library"]


# --- runs -------------------------------------------------------------------------------------------------------

def _read(run_dir: Path) -> dict:
    with _lock:
        return json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))


def _write(run_dir: Path, run: dict) -> dict:
    with _lock:
        tmp = run_dir / (RUN_FILE + ".tmp")
        tmp.write_text(json.dumps(run), encoding="utf-8")
        os.replace(tmp, run_dir / RUN_FILE)
        return run


def _update(run_dir: Path, **changes) -> dict:
    with _lock:
        return _write(run_dir, {**_read(run_dir), **changes})


def _claim(run_dir: Path) -> bool:
    """Take the run's one upload: True for the first request only."""
    with _lock:
        run = _read(run_dir)
        if run["status"] != "waiting":
            return False
        _write(run_dir, {**run, "status": "receiving"})
        return True


def _link(run: dict, *tail: str) -> str:
    """The run's private page (or a file under it): whoever has the link can upload the deck and download the copy
    until the run expires."""
    return "/".join([config.PUBLIC_URL, "runs", run["id"], run["token"], *tail])


def _caller() -> str | None:
    """The signed-in designer (their Entra object id); None without per-person sign-in."""
    token = get_access_token()
    return token.subject if token else None


def _run(run_id: str, caller: str | None) -> tuple[Path, dict]:
    """A live run the caller may use. With Entra sign-in, a run belongs to whoever started it."""
    run_dir = config.WORK_DIR / run_id
    if RUN_ID.fullmatch(run_id) and (run_dir / RUN_FILE).exists():
        run = _read(run_dir)
        if run["expires"] > time.time() and run.get("owner") == caller:
            return run_dir, run
    raise ToolError(f"no run '{run_id}' (runs expire after {config.RUN_TTL_HOURS:g} hours): "
                    "start a new one with start_icon_run")


def _cleanup():
    """Delete runs whose time is up: their decks, copies and links go with them."""
    now = time.time()
    for path in config.WORK_DIR.glob(f"*/{RUN_FILE}"):
        try:
            expired = json.loads(path.read_text(encoding="utf-8"))["expires"] < now
        except (OSError, ValueError, KeyError):
            continue
        if expired:
            shutil.rmtree(path.parent, ignore_errors=True)


_sweeping = threading.Event()


def _sweep():
    """Clean up every hour too, so client decks don't outstay their run on a quiet server."""
    while True:
        try:
            _cleanup()
        except OSError:
            log.exception("cleaning up expired runs failed")
        time.sleep(3600)


def _process(run_dir: Path):
    """Run the uploaded deck (in its own thread; PARALLEL_RUNS at a time, the rest wait their turn)."""
    with _slots:
        run = _read(run_dir)
        try:
            result = jobs.run(run_dir, run["deck_file"], _library(), run["mode"], run["slides"])
        except (ValueError, NotImplementedError) as e:
            _update(run_dir, status="failed", error=str(e))
        except Exception:   # a file python-pptx can't open, or a bug: the designer gets a reason, the log the details
            log.exception("run %s failed", run_dir.name)
            _update(run_dir, status="failed", error=FAILED)
        else:
            _update(run_dir, status="done", copy_icons=None if run["mode"] == "suggest" else len(result["picks"]))


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LABEL else text[:LABEL - 1] + "…"


def _results(run_dir: Path, run: dict) -> dict:
    """A finished run in short: the icons placed (or suggested), the flagged items with the closest icons for each
    placeholder a designer can fill, the report's notes and the copy's download link."""
    rep, lib = jobs.saved(run_dir), _library()
    settings = rep["settings"]
    icons = [{"pick": n, "slide": p["item"]["slide"], "label": _label(p["item"]["text"]), "icon": p["icon"]["id"],
              "draws": p["icon"]["description"], "color": f"{p['color_slot']} #{p['color_hex']}", "why": p["reason"]}
             for n, p in enumerate(rep["picks"])]
    flagged = [{"flag": n, "slide": f["item"]["slide"], "label": _label(f["item"]["text"]), "reason": f["reason"],
                "can_choose": bool(f.get("spot")),
                "options": [{"icon": i, "draws": icon.description} for i in f.get("options", [])
                            if (icon := lib.get(i))]}
               for n, f in enumerate(rep["flags"])]
    has_copy = jobs.output(run_dir) is not None
    return {"run_id": run["id"], "status": "done", "mode": run["mode"], "style": settings["style"],
            "deck_color": f"{settings['color_slot']} #{settings['color_hex']}",
            "suggested" if run["mode"] == "suggest" else "placed": icons, "flagged": flagged, "notes": rep["gaps"],
            "copy": {"download": _link(run, "download"), "icons": run["copy_icons"]} if has_copy else None,
            "next": "Report the results. For flagged placeholders that can_choose, offer their options and call "
                    "choose_icons with the designer's picks" + (
                        "; in suggest mode, ask which suggestions to add." if run["mode"] == "suggest" else ".")}


# --- tools ------------------------------------------------------------------------------------------------------

def start_icon_run(mode: Literal["replace", "add", "suggest"] = "replace", slides: str = "") -> dict:
    """Start placing icons from Prezlab's approved library in a PowerPoint deck. Returns a private upload link for
    the designer: they open it and upload the .pptx there (decks never pass through the chat). Then call
    get_icon_run.

    mode: "replace" (default) swaps the PL logo placeholders designers left for icons, and changes nothing else;
    "add" also finds spots for icons on slides without placeholders; "suggest" plans icons without adding any
    (choose_icons then adds the ones the designer picks).
    slides: only these slide numbers, comma-separated (e.g. "2,3,5"); empty for the whole deck."""
    if slides.strip() and not SLIDES.fullmatch(slides):
        raise ToolError('slides: slide numbers separated by commas, e.g. "2,3,5", or empty for the whole deck')
    _cleanup()
    run_id = uuid.uuid4().hex[:12]
    run_dir = config.WORK_DIR / run_id
    run_dir.mkdir(parents=True)
    now = time.time()
    run = _write(run_dir, {
        "id": run_id, "token": secrets.token_urlsafe(24), "mode": mode, "status": "waiting", "error": "",
        "slides": [int(s) for s in slides.split(",") if s.strip()] or None, "deck_file": "", "copy_icons": None,
        "owner": _caller(), "created": now, "expires": now + config.RUN_TTL_HOURS * 3600})
    return {"run_id": run_id, "status": "waiting", "upload_link": _link(run),
            "link_expires_in_hours": config.RUN_TTL_HOURS,
            "next": "Give the designer the upload link. When they say the deck is uploaded, call get_icon_run."}


def get_icon_run(run_id: str) -> dict:
    """Where an icon run is. waiting: the designer hasn't uploaded the deck yet. running: call again in a few
    seconds. failed: the reason, for the designer. done: the icons placed (or suggested), the items flagged with
    the reason and, for placeholders a designer can fill, the closest icons; and the new copy's download link."""
    run_dir, run = _run(run_id, _caller())
    status = "running" if run["status"] == "receiving" else run["status"]
    if status == "waiting":
        return {"run_id": run_id, "status": status, "upload_link": _link(run),
                "next": "The deck isn't uploaded yet: ask the designer to upload it with the link."}
    if status == "running":
        return {"run_id": run_id, "status": status, "next": "Still running: call get_icon_run again in a few seconds."}
    if status == "failed":
        return {"run_id": run_id, "status": status, "error": run["error"],
                "next": "Tell the designer why; after fixing the deck they can start a new run."}
    return _results(run_dir, run)


async def choose_icons(run_id: str, choices: dict[str, str] | None = None, picks: list[int] | None = None) -> dict:
    """Make a new copy of the run's deck with the icons the designer chose, and return its download link. The
    original deck is never changed; this run's earlier copy is replaced.

    choices: flagged placeholder number ("flag" in get_icon_run) -> icon id, from that placeholder's options or
    search_icons. Only placeholders that can_choose take one.
    picks: suggestion numbers ("pick" in get_icon_run) to add, in suggest mode. In replace and add mode the icons
    already placed stay; pass picks only to keep fewer of them."""
    run_dir, run = _run(run_id, _caller())
    if run["status"] != "done":
        raise ToolError(f"the run isn't finished ({run['status']}): call get_icon_run first")
    try:
        chosen = {int(k): v for k, v in (choices or {}).items()}
    except ValueError:
        raise ToolError('choices: flagged placeholder numbers to icon ids, e.g. {"0": "target"}')
    if picks is None:
        picks = [] if run["mode"] == "suggest" else list(range(len(jobs.saved(run_dir)["picks"])))
    try:
        result = await anyio.to_thread.run_sync(jobs.apply, run_dir, _library(), picks, chosen)
    except jobs.RunError as e:
        raise ToolError(str(e))
    _update(run_dir, copy_icons=result["added"])
    return {"run_id": run_id, "icons_in_copy": result["added"], "download": _link(run, "download"),
            "not_added": [f"{i}: no longer approved in the library" for i in result["skipped"]],
            "next": "Give the designer the download link (the same link now serves this new copy)."}


def search_icons(query: str, limit: int = 10) -> dict:
    """Search Prezlab's approved icon library by meaning, with English or Arabic keywords (e.g. "growth",
    "cyber security", "نمو"). Use it for a flagged placeholder when none of its options fit. Returns icon ids and
    what each draws, the house pick first."""
    lib = _library()
    limit = max(1, min(limit, 25))
    arabic = bool(re.search(r"[؀-ۿ]", query))
    found = []
    if key := lib.lookup(query):
        found += [lib.concept_map[key]["id"], *lib.concept_map[key]["alts"]]
    for style in sorted({i.style for i in lib.icons.values()}):
        found += [i.id for i in lib.search(query, style, arabic, limit)]
    icons = [icon for i in dict.fromkeys(found) if (icon := lib.get(i))][:limit]
    return {"query": query,
            "icons": [{"icon": i.id, "family": i.family, "draws": i.description, "keywords": i.tags[:8]}
                      for i in icons],
            "next": "" if icons else "Nothing matched: try one simple keyword, or what the icon should draw."}


TOOLS = [
    (start_icon_run, ToolAnnotations(title="Start an icon run", read_only_hint=False, destructive_hint=False,
                                     idempotent_hint=False, open_world_hint=False)),
    (get_icon_run, ToolAnnotations(title="Check an icon run", read_only_hint=True, open_world_hint=False)),
    (choose_icons, ToolAnnotations(title="Add chosen icons", read_only_hint=False, destructive_hint=False,
                                   idempotent_hint=True, open_world_hint=False)),
    (search_icons, ToolAnnotations(title="Search the icon library", read_only_hint=True, open_world_hint=False)),
]


# --- run pages (what the designer opens from the link) ----------------------------------------------------------

PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
MODE_TEXT = {"replace": "replace the PL logo placeholders with icons from the Prezlab library",
             "add": "add icons from the Prezlab library where they fit",
             "suggest": "suggest icons from the Prezlab library (Claude then adds the ones you choose)"}
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">%REFRESH%<title>Prezlab Icons</title><style>
:root { color-scheme: light dark; --bg: #f6f6f4; --card: #ffffff; --text: #1d1d1b; --muted: #66665f; --line: #e2e2dc;
  --accent: #1d1d1b; --on-accent: #ffffff; --bad: #a3262a; }
@media (prefers-color-scheme: dark) { :root { --bg: #151514; --card: #1e1e1d; --text: #ededea; --muted: #a5a59e;
  --line: #34342f; --accent: #ededea; --on-accent: #151514; --bad: #f08a8d; } }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 560px; margin: 48px auto; padding: 0 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 24px; }
h1 { font-size: 20px; margin: 0 0 8px; } p, li { color: var(--muted); } .bad { color: var(--bad); }
.button, button { display: inline-block; background: var(--accent); color: var(--on-accent); border: 0; border-radius: 8px;
  padding: 10px 16px; font: inherit; font-weight: 600; text-decoration: none; cursor: pointer; }
input[type=file] { display: block; margin: 16px 0; font: inherit; color: var(--text); max-width: 100%; }
small { color: var(--muted); }
</style></head><body><main><div class="card">%BODY%</div></main></body></html>"""


def _page(body: str, status: int = 200, refresh: bool = False) -> HTMLResponse:
    page = PAGE.replace("%REFRESH%", '<meta http-equiv="refresh" content="3">' if refresh else "")
    return HTMLResponse(page.replace("%BODY%", body), status, headers=PRIVATE)


def _gone() -> HTMLResponse:
    return _page("<h1>This link has expired</h1><p>Runs and their files are deleted after "
                 f"{config.RUN_TTL_HOURS:g} hours. Ask Claude to start a new run.</p>", 404)


def _link_run(request: Request) -> tuple[Path, dict] | None:
    """The run behind a private link, while the link is valid."""
    run_id, token = request.path_params["run_id"], request.path_params["token"]
    run_dir = config.WORK_DIR / run_id
    if not RUN_ID.fullmatch(run_id) or not (run_dir / RUN_FILE).exists():
        return None
    run = _read(run_dir)
    if not hmac.compare_digest(run["token"].encode(), token.encode()) or run["expires"] < time.time():
        return None
    return run_dir, run


def _upload_form(run: dict, error: str = "", status: int = 200) -> HTMLResponse:
    return _page(f"""<h1>Upload your deck</h1>
<p>Prezlab Icons will {MODE_TEXT[run['mode']]} and give you a new copy. Your original file isn't changed.</p>
{f'<p class="bad">{html.escape(error)}</p>' if error else ''}
<form method="post" enctype="multipart/form-data"><input type="file" name="deck" accept=".pptx" required>
<button type="submit">Upload and run</button></form>
<p><small>Save the deck in PowerPoint first. This link is private to this run and stops working after
{config.RUN_TTL_HOURS:g} hours, when the deck and its copy are deleted.</small></p>""", status)


async def run_page(request: Request) -> Response:
    """GET: the run's page (upload form, progress, result). POST: the deck upload, which starts the run."""
    found = _link_run(request)
    if found is None:
        return _gone()
    run_dir, run = found
    if request.method == "POST":
        return await _receive(request, run_dir, run)
    status = run["status"]
    if status == "waiting":
        return _upload_form(run)
    if status in ("receiving", "running"):
        return _page("<h1>Placing icons…</h1><p>This page updates by itself. You can go back to Claude: it sees the "
                     "result too.</p>", refresh=True)
    if status == "failed":
        return _page(f"<h1>This run didn't finish</h1><p class='bad'>{html.escape(run['error'])}</p>"
                     "<p>Ask Claude to start a new run.</p>")
    rep = jobs.saved(run_dir)
    flagged = [f for f in rep["flags"] if f.get("spot")]
    items = "".join(f"<li>Slide {f['item']['slide']}, {html.escape(_label(f['item']['text']))}: "
                    f"{html.escape(f['reason'])}</li>" for f in flagged)
    more = (f"<p>{len(flagged)} placeholder{'s' if len(flagged) != 1 else ''} left for you to choose:</p><ul>{items}"
            "</ul><p>Back in Claude, ask it to go through them with you.</p>") if flagged else ""
    if jobs.output(run_dir) is None:   # suggest mode: nothing added yet
        return _page(f"<h1>Suggestions ready</h1><p>{len(rep['picks'])} icons suggested. Go back to Claude to choose "
                     f"which ones to add.</p>{more}")
    icons = run["copy_icons"] or 0
    return _page(f"<h1>Done</h1><p>{icons} icon{'s' if icons != 1 else ''} in the new copy.</p>"
                 f"<p><a class='button' href='{html.escape(_link(run, 'download'))}'>Download the new copy</a></p>{more}")


async def _receive(request: Request, run_dir: Path, run: dict) -> Response:
    """Save the uploaded deck and start the run. One upload per run."""
    if run["status"] != "waiting":
        return RedirectResponse(_link(run), 303)
    form = await request.form(max_files=1, max_fields=1)
    deck = form.get("deck")
    if not isinstance(deck, UploadFile) or not (deck.filename or "").lower().endswith(".pptx"):
        return _upload_form(run, "Choose a PowerPoint deck (.pptx).", 400)
    if await deck.read(4) != b"PK\x03\x04":   # every .pptx is a zip
        return _upload_form(run, "This file isn't a PowerPoint deck: save it from PowerPoint as a .pptx.", 400)
    await deck.seek(0)
    if not _claim(run_dir):
        return RedirectResponse(_link(run), 303)
    name = re.sub(r"[^\w\- ]+", "_", Path(deck.filename).stem).strip(" _")[:80] or "deck"
    path = run_dir / f"{name}.pptx"
    try:
        with path.open("wb") as f:
            shutil.copyfileobj(deck.file, f)
    except OSError:
        log.exception("run %s: saving the upload failed", run["id"])
        path.unlink(missing_ok=True)
        _update(run_dir, status="waiting")
        return _upload_form(run, "The upload didn't save. Try again.", 500)
    if path.stat().st_size > config.MAX_DECK_MB * 2**20:
        path.unlink()
        _update(run_dir, status="waiting")
        return _upload_form(run, f"This deck is over {config.MAX_DECK_MB} MB. Compress its pictures in PowerPoint "
                                 "(File > Compress Pictures) and try again.", 413)
    run = _update(run_dir, status="running", deck_file=path.name)
    threading.Thread(target=_process, args=(run_dir,), daemon=True, name=f"run-{run['id']}").start()
    return RedirectResponse(_link(run), 303)


async def download(request: Request) -> Response:
    found = _link_run(request)
    if found is None:
        return _gone()
    out = jobs.output(found[0])
    if out is None:
        return _page("<h1>No copy yet</h1><p>Go back to Claude to choose which icons to add.</p>", 404)
    return FileResponse(out, filename=out.name, headers=PRIVATE)


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "icons": len(_library().icons)})


# --- app --------------------------------------------------------------------------------------------------------

def build_app(mode: str | None = None):
    """The connector: the MCP endpoint at /mcp behind the sign-in mode (auth.py), and the run pages."""
    mode = mode or auth.mode()
    mcp_url = config.PUBLIC_URL + MCP_PATH
    server = MCPServer("prezlab-icons", title="Prezlab Icons", instructions=INSTRUCTIONS,
                       **(auth.entra(mcp_url) if mode == "entra" else {}))
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations)
    server.custom_route("/runs/{run_id}/{token}", ["GET", "POST"])(run_page)
    server.custom_route("/runs/{run_id}/{token}/download", ["GET"])(download)
    server.custom_route("/health", ["GET"])(health)
    url = urlparse(config.PUBLIC_URL)
    local = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    security = TransportSecuritySettings(   # Host and Origin checks against DNS rebinding
        enable_dns_rebinding_protection=True, allowed_hosts=[url.netloc, *local],
        allowed_origins=[f"{url.scheme}://{url.netloc}", "https://claude.ai", "https://pivot.claude.ai",
                         "http://127.0.0.1:*", "http://localhost:*"])
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
                                     transport_security=security)
    if not _sweeping.is_set():
        _sweeping.set()
        threading.Thread(target=_sweep, daemon=True, name="run-cleanup").start()
    return auth.RequireToken(app, MCP_PATH, config.MCP_TOKEN) if mode == "token" else app


app = build_app()
