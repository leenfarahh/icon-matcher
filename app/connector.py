"""Claude connector: a remote MCP server that lets Claude (chat, Cowork, Claude Code and the PowerPoint add-in) run the
icon placer on a deck and hand back a new copy. Run: uvicorn app.connector:app --port 8001

start_icon_run opens the tool in a panel inside the conversation (an MCP App): the local test console
(static/index.html) for one run, where the designer uploads the deck, reviews the picks, chooses icons for flagged
placeholders on their slides and downloads the copy. Where Claude shows no panel, the run's private link opens the same
page in the browser. Decks never pass through the chat: the page sends them straight to this server. Claude follows
along with get_icon_run and can add icons from the chat with choose_icons. The original deck is never changed. Runs
(deck, copy, link) are deleted after ICON_RUN_TTL_HOURS. Sign-in: auth.py (designers use their Odoo login: oauth.py).
Hosting: connector-setup.md."""
import hmac
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
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import auth, config, jobs, oauth, pages
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
new copy; the designer's original file is never changed. The deck goes straight to the service from the Prezlab Icons
page, never through this chat.

1. Call start_icon_run (mode "replace" unless the designer asks for something else). In Claude web, desktop and
   mobile it opens the Prezlab Icons panel in the conversation: the designer uploads the deck there and does the rest
   in it (the picks, icons for flagged placeholders, the download). Tell them in one line to upload their deck in the
   panel. Where no panel shows (for example the PowerPoint add-in or Claude Code), give them the run's link instead:
   it opens the same page in the browser. In PowerPoint, ask them to save the deck first and upload the saved .pptx.
2. The panel tells you when they run the deck or add icons. Call get_icon_run for the details when they ask, or to
   follow a run they do at the link.
3. If they'd rather choose in the chat: offer each flagged placeholder's options by what the icon draws, then call
   choose_icons with their picks. Use search_icons when none fit. Use only icon ids these tools return.

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
    """Start running the run's deck: False while it is already running (a second click on Run)."""
    with _lock:
        run = _read(run_dir)
        if run["status"] == "running":
            return False
        _write(run_dir, {**run, "status": "running", "error": ""})
        return True


def _link(run: dict, *tail: str) -> str:
    """The run's private page (or an address under it): whoever has the link can run a deck and download the copy
    until the run expires."""
    return "/".join([config.PUBLIC_URL, "runs", run["id"], run["token"], *tail])


def _caller() -> str | None:
    """The signed-in designer (their Odoo email); None without per-person sign-in."""
    token = get_access_token()
    return token.subject if token else None


def _run(run_id: str, caller: str | None) -> tuple[Path, dict]:
    """A live run the caller may use. With Odoo sign-in, a run belongs to whoever started it."""
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


def _process(run_dir: Path, deck_file: str, mode: str, slides: list[int] | None) -> dict:
    """Run the deck (in a worker thread; PARALLEL_RUNS at a time, the rest wait their turn) and record how it went."""
    with _slots:
        try:
            result = jobs.run(run_dir, deck_file, _library(), mode, slides)
        except (ValueError, NotImplementedError) as e:
            _update(run_dir, status="failed", error=str(e))
            raise jobs.RunError(str(e))
        except Exception:   # a file python-pptx can't open, or a bug: the designer gets a reason, the log the details
            log.exception("run %s failed", run_dir.name)
            _update(run_dir, status="failed", error=FAILED)
            raise jobs.RunError(FAILED[0].upper() + FAILED[1:] + ".", 422)
    _update(run_dir, status="done", copy_icons=None if mode == "suggest" else len(result["picks"]))
    return result


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
            "copy": {"download": _link(run, "jobs", run["id"], "download"), "icons": run["copy_icons"]}
            if has_copy else None,
            "next": "Report the results briefly. The designer can choose icons for flagged placeholders and download "
                    "the copy in the Prezlab Icons panel (or at the link); if they'd rather choose here, offer the "
                    "options of the placeholders that can_choose and call choose_icons with their picks" + (
                        "; in suggest mode, ask which suggestions to add." if run["mode"] == "suggest" else ".")}


# --- tools ------------------------------------------------------------------------------------------------------

def start_icon_run(mode: Literal["replace", "add", "suggest"] = "replace", slides: str = "") -> CallToolResult:
    """Open Prezlab Icons for a PowerPoint deck: it places icons from Prezlab's approved library and returns a new
    copy. In Claude web, desktop and mobile this shows the Prezlab Icons panel in the conversation, where the designer
    uploads the deck, reviews the picks, chooses icons for flagged placeholders and downloads the copy. Also returns
    the run's private link, which opens the same page in the browser, for clients that show no panel.

    mode: "replace" (default) swaps the PL logo placeholders designers left for icons, and changes nothing else;
    "add" also finds spots for icons on slides without placeholders; "suggest" plans icons without adding any (the
    designer then adds the ones they pick). The designer can change it in the panel.
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
    link = _link(run)
    told = {"run_id": run_id, "status": "waiting", "link": link, "link_expires_in_hours": config.RUN_TTL_HOURS,
            "next": "The Prezlab Icons panel shows with this result in Claude web, desktop and mobile: tell the designer "
                    "in one line to upload their deck there; don't paste the link. Only where no panel shows (the "
                    "PowerPoint add-in, Claude Code), give them the link: it opens the same page in the browser."}
    # The panel reads structuredContent (Claude doesn't): the address it works against.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(told, indent=2))],
                          structured_content={"run_id": run_id, "base": link + "/", "link": link, "mode": mode})


def get_icon_run(run_id: str) -> dict:
    """Where an icon run is. waiting: the designer hasn't run a deck yet. running: call again in a few seconds.
    failed: the reason, for the designer. done: the icons placed (or suggested), the items flagged with the reason
    and, for placeholders a designer can fill, the closest icons; and the new copy's download link."""
    run_dir, run = _run(run_id, _caller())
    status = run["status"]
    if status == "waiting":
        return {"run_id": run_id, "status": status, "link": _link(run),
                "next": "No deck yet: the designer uploads it in the Prezlab Icons panel (or at the link)."}
    if status == "running":
        return {"run_id": run_id, "status": status, "next": "Still running: call get_icon_run again in a few seconds."}
    if status == "failed":
        return {"run_id": run_id, "status": status, "error": run["error"],
                "next": "Tell the designer why; after fixing the deck they can run it again in the panel."}
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
    return {"run_id": run_id, "icons_in_copy": result["added"], "download": _link(run, "jobs", run_id, "download"),
            "not_added": [f"{i}: no longer approved in the library" for i in result["skipped"]],
            "next": "Tell the designer the new copy is ready: Download deck in the panel gets it, or this link."}


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


PANEL_TOOL = (start_icon_run, ToolAnnotations(title="Open Prezlab Icons", read_only_hint=False, destructive_hint=False,
                                              idempotent_hint=False, open_world_hint=False))
TOOLS = [
    (get_icon_run, ToolAnnotations(title="Check an icon run", read_only_hint=True, open_world_hint=False)),
    (choose_icons, ToolAnnotations(title="Add chosen icons", read_only_hint=False, destructive_hint=False,
                                   idempotent_hint=True, open_world_hint=False)),
    (search_icons, ToolAnnotations(title="Search the icon library", read_only_hint=True, open_world_hint=False)),
]


# --- the page: in Claude's panel, or at the run's private link --------------------------------------------------
# One page for both: the local test console (static/index.html) working against one run through the endpoints below,
# under the run's private link. Library editing stays in the local tool: on Render it would be lost at the next deploy.

STATIC = Path(__file__).parent / "static"
HOST_MARK = "<!-- HOST -->"   # where the page takes its hosting script
PANEL_URI = "ui://prezlab-icons/run.html"
APPS_CLIENT = "vendor/ext-apps-app-with-deps-1.7.5.js"   # the MCP Apps client (static/vendor/README.md)
PANEL_ORIGIN = re.compile(r"https://[a-z0-9-]+\.claudemcpcontent\.com")   # where Claude runs panels
MODES = ("replace", "add", "suggest")
MAX_ICON_MB = 5
EXPIRED = (f"This run has expired (runs and their files are deleted after {config.RUN_TTL_HOURS:g} hours). "
           "Ask Claude to start a new one.")
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"   # an icon opened on its own runs nothing


def _page(host: str) -> str:
    """The page with a hosting script in place of its HOST marker."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    if HOST_MARK not in page:
        raise RuntimeError(f"static/index.html has lost its {HOST_MARK} marker")
    return page.replace(HOST_MARK, host, 1)


def panel_html() -> str:
    """The panel Claude shows: the page, the MCP Apps client and the bridge (panel.js), all inline, since a panel can't
    load scripts from anywhere. The client is an ES module; inside one async function, with its export statement
    turned into a name, it runs as a plain script, as Image Studio's panel does."""
    client = (STATIC / APPS_CLIENT).read_text(encoding="utf-8")
    exports = re.search(r"export\s*\{([^}]*)\}\s*;?\s*$", client)
    name = exports and re.search(r"([\w$]+) as App\b", exports.group(1))
    if not name:
        raise RuntimeError(f"{APPS_CLIENT} exports no App: vendor the client again (static/vendor/README.md)")
    bridge = (STATIC / "panel.js").read_text(encoding="utf-8")
    script = f"(async () => {{\n{client[:exports.start()]}\nconst App = {name.group(1)};\n{bridge}\n}})();"
    host = ('window.HOST = (() => { let resolve; const ready = new Promise(r => resolve = r); '
            'return {mode: "panel", base: "", ready, resolve}; })();')
    script = script.replace("</script", "<\\/script")   # a literal </script> in the code would end the tag early
    return _page(f"<script>{host}</script>\n<script>{script}</script>")


def _gone() -> HTMLResponse:
    return pages.render(f"<h1>This link has expired</h1><p>{EXPIRED}</p>", 404)


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


async def run_page(request: Request) -> Response:
    """The page at the run's private link, for a browser (where Claude shows no panel, or to open it full size)."""
    found = _link_run(request)
    if found is None:
        return _gone()
    run = found[1]
    base = json.dumps(f"/runs/{run['id']}/{run['token']}/")
    host = f'<script>window.HOST = {{mode: "page", base: {base}, ready: Promise.resolve()}};</script>'
    return HTMLResponse(_page(host), headers=pages.PRIVATE)


def _api(handler):
    """One of the page's endpoints under the run's private link: the run behind the link (else 404), errors as the
    page reads them ({"detail": ...}), never cached, and answered to Claude's panel origin too. Preflight requests
    (the page's JSON posts) are answered here."""
    async def endpoint(request: Request) -> Response:
        origin = request.headers.get("origin", "")
        cors = {"Access-Control-Allow-Origin": origin} if PANEL_ORIGIN.fullmatch(origin) else {}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={**cors, "Vary": "Origin", "Access-Control-Max-Age": "600",
                                                      "Access-Control-Allow-Methods": "GET, POST",
                                                      "Access-Control-Allow-Headers": "Content-Type"})
        found, job = _link_run(request), request.path_params.get("job")
        try:
            if found is None or job not in (None, found[1]["id"]):
                raise jobs.RunError(EXPIRED, 404)
            response = await handler(request, *found)
        except jobs.RunError as e:
            response = JSONResponse({"detail": e.detail}, e.status)
        headers = {**pages.PRIVATE, **cors, "Vary": "Origin"}
        if "content-security-policy" in response.headers:   # an icon's own, stricter policy stands
            headers.pop("Content-Security-Policy")
        response.headers.update(headers)
        return response
    return endpoint


def _svg(svg: bytes) -> Response:
    return Response(svg, media_type="image/svg+xml", headers={"Content-Security-Policy": SVG_CSP})


def _for_page(run: dict, result: dict) -> dict:
    """A run's report as the page reads it: its job is the run, and its copy is named, not located on the server."""
    out = result.get("output_file")
    return {**result, "job": run["id"], "output_file": Path(out).name if out else None}


async def _state(request: Request, run_dir: Path, run: dict) -> Response:
    """What the page opens with: the mode and slides Claude set, and the run's latest result."""
    result = _for_page(run, jobs.saved(run_dir)) if run["status"] == "done" else None
    return JSONResponse({"status": run["status"], "error": run["error"], "mode": run["mode"],
                         "slides": ",".join(map(str, run["slides"] or [])), "result": result})


async def _library_index(request: Request, run_dir: Path, run: dict) -> Response:
    return JSONResponse(jobs.library_index(_library()))


async def _icon(request: Request, run_dir: Path, run: dict) -> Response:
    lib = _library()
    icon = lib.all.get(request.path_params["icon_id"])
    if icon is None:
        raise jobs.RunError("unknown icon", 404)
    return _svg(jobs.icon_svg(lib.svg_bytes(icon), request.query_params.get("color", "")))


def _clear(run_dir: Path):
    """Everything from the run's last deck (the deck, its report and copy, uploaded icons, slide pictures)."""
    for path in run_dir.iterdir():
        if not path.name.startswith(RUN_FILE):
            shutil.rmtree(path) if path.is_dir() else path.unlink()


async def _run_deck(request: Request, run_dir: Path, run: dict) -> Response:
    """The page's Run: the deck is saved, run and its report returned, as the test console's POST /jobs does.
    Running again starts the run over with the new deck."""
    form = await request.form(max_files=1, max_fields=3)
    deck, mode, slides = form.get("deck"), str(form.get("mode") or run["mode"]), str(form.get("slides") or "")
    if not isinstance(deck, UploadFile) or not (deck.filename or "").lower().endswith(".pptx"):
        raise jobs.RunError("Choose a PowerPoint deck (.pptx).")
    if await deck.read(4) != b"PK\x03\x04":   # every .pptx is a zip
        raise jobs.RunError("This file isn't a PowerPoint deck: save it from PowerPoint as a .pptx.")
    await deck.seek(0)
    if mode not in MODES:
        raise jobs.RunError(f"Mode: one of {', '.join(MODES)}.")
    if slides.strip() and not SLIDES.fullmatch(slides):
        raise jobs.RunError("Slides: slide numbers separated by commas, e.g. 2,3,5, or empty for the whole deck.")
    if not _claim(run_dir):
        raise jobs.RunError("This run is still placing icons: wait for it to finish.", 409)
    try:
        name = (re.sub(r"[^\w\- ]+", "_", Path(deck.filename).stem).strip(" _")[:80] or "deck") + ".pptx"
        _clear(run_dir)
        with (run_dir / name).open("wb") as f:
            shutil.copyfileobj(deck.file, f)
        if (run_dir / name).stat().st_size > config.MAX_DECK_MB * 2**20:
            raise jobs.RunError(f"This deck is over {config.MAX_DECK_MB} MB. Compress its pictures in PowerPoint "
                                "(File > Compress Pictures) and try again.", 413)
    except (OSError, jobs.RunError) as e:
        _clear(run_dir)
        _update(run_dir, status="failed", error=str(e) if isinstance(e, jobs.RunError) else "the upload didn't save")
        if isinstance(e, jobs.RunError):
            raise
        log.exception("run %s: saving the upload failed", run["id"])
        raise jobs.RunError("The upload didn't save. Try again.", 500)
    slide_list = [int(n) for n in slides.split(",") if n.strip()] or None
    _update(run_dir, mode=mode, slides=slide_list, deck_file=name)
    result = await anyio.to_thread.run_sync(_process, run_dir, name, mode, slide_list)
    return JSONResponse(_for_page(run, result))


async def _apply(request: Request, run_dir: Path, run: dict) -> Response:
    """The page's Add selected, Add all and Add chosen: a new copy with exactly these icons."""
    try:
        body = await request.json()
        picks = [int(n) for n in body.get("picks") or []]
        choices = {int(k): str(v) for k, v in (body.get("choices") or {}).items()}
    except (ValueError, TypeError, AttributeError):
        raise jobs.RunError("picks: suggestion numbers; choices: placeholder numbers to icon ids")
    if run["status"] != "done":
        raise jobs.RunError("Run the deck first.")
    result = await anyio.to_thread.run_sync(jobs.apply, run_dir, _library(), picks, choices)
    _update(run_dir, copy_icons=result["added"])
    return JSONResponse({**result, "download": f"jobs/{run['id']}/download"})


async def _upload_icon(request: Request, run_dir: Path, run: dict) -> Response:
    """An icon the designer brings for a flagged placeholder (jobs.save_upload)."""
    form = await request.form(max_files=1, max_fields=0)
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise jobs.RunError("Choose an icon file: an SVG, or a PNG or JPG to convert.")
    data = await file.read(MAX_ICON_MB * 2**20 + 1)
    if len(data) > MAX_ICON_MB * 2**20:
        raise jobs.RunError(f"This icon file is over {MAX_ICON_MB} MB.", 413)
    return JSONResponse(await anyio.to_thread.run_sync(jobs.save_upload, run_dir, data, file.filename or "icon"))


async def _uploaded_icon(request: Request, run_dir: Path, run: dict) -> Response:
    path = jobs.upload_path(run_dir, f"upload:{request.path_params['digest']}")
    if path is None or not path.exists():
        raise jobs.RunError("no such upload", 404)
    return _svg(jobs.icon_svg(path.read_bytes(), request.query_params.get("color", "")))


async def _slide(request: Request, run_dir: Path, run: dict) -> Response:
    path = await anyio.to_thread.run_sync(jobs.slide_picture, run_dir, request.path_params["n"])
    return FileResponse(path, media_type="image/png")


async def _download(request: Request, run_dir: Path, run: dict) -> Response:
    out = jobs.output(run_dir)
    if out is None:
        raise jobs.RunError("There's no copy yet: add icons first.", 404)
    return FileResponse(out, filename=out.name)


RUN_LINK = "/runs/{run_id}/{token}"
API = [("/state", ["GET"], _state), ("/library", ["GET"], _library_index), ("/icons/{icon_id}.svg", ["GET"], _icon),
       ("/jobs", ["POST"], _run_deck), ("/jobs/{job}/apply", ["POST"], _apply),
       ("/jobs/{job}/uploads", ["POST"], _upload_icon), ("/jobs/{job}/uploads/{digest}.svg", ["GET"], _uploaded_icon),
       ("/jobs/{job}/slides/{n:int}.png", ["GET"], _slide), ("/jobs/{job}/download", ["GET"], _download)]


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "icons": len(_library().icons)})


# --- app --------------------------------------------------------------------------------------------------------

def build_app(mode: str | None = None):
    """The connector: the MCP endpoint at /mcp behind the sign-in mode (auth.py) with its tools and the Prezlab Icons
    panel, the page and its endpoints under each run's private link and, with Odoo sign-in, the OAuth endpoints and
    sign-in page."""
    mode = mode or auth.mode()
    mcp_url = config.PUBLIC_URL + MCP_PATH
    url = urlparse(config.PUBLIC_URL)
    origin = f"{url.scheme}://{url.netloc}"
    provider = oauth.Provider(oauth.Store(config.AUTH_DB), config.PUBLIC_URL, mcp_url,
                              oauth.sealing_key(config.ODOO_TD_ENCRYPTION_KEY)) if mode == "odoo" else None
    apps = Apps()   # the panel: start_icon_run shows it; it may reach this server only (CSP), for data and icons
    apps.add_html_resource(PANEL_URI, panel_html(), name="Prezlab Icons",
                           description="Run a deck, review the picks, choose icons for flagged placeholders, download.",
                           csp=ResourceCsp(connect_domains=[origin], resource_domains=[origin]))
    fn, annotations = PANEL_TOOL
    apps.tool(resource_uri=PANEL_URI, annotations=annotations)(fn)
    server = MCPServer("prezlab-icons", title="Prezlab Icons", instructions=INSTRUCTIONS, extensions=[apps],
                       **(oauth.server_settings(provider) if provider else {}))
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations)
    if provider:
        server.custom_route("/signin/{request_id}", ["GET", "POST"])(provider.signin)
    server.custom_route(RUN_LINK, ["GET"])(run_page)
    for path, methods, handler in API:
        server.custom_route(RUN_LINK + path, [*methods, "OPTIONS"])(_api(handler))
    server.custom_route("/health", ["GET"])(health)
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
