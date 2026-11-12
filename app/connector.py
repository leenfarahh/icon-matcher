"""Claude connector: a remote MCP server that lets Claude (chat, Cowork, Claude Code and the PowerPoint add-in) run the
icon placer on a deck and hand back a new copy. Run: uvicorn app.connector:app --port 8001

start_icon_run opens the tool in a panel inside the conversation (an MCP App): the local test console
(static/index.html) for one run, where the designer uploads the deck, reviews the picks, chooses icons for flagged
placeholders on their slides and downloads the copy. Where Claude shows no panel, the run's private link opens the same
page in the browser. Decks never pass through the chat: the page sends them straight to this server, and a deck the
designer attached to their message is sent by Claude from its code tools (attached_deck) into the open page. Claude
follows along with get_icon_run and can add icons from the chat with choose_icons. The original deck is never changed.
A run (deck, copy, link) is deleted after ICON_RUN_IDLE_HOURS unused, and a web page run as soon as the designer
leaves its page. Designers never sign in: the hosted connector's address
carries a secret instead (auth.py). People without Claude sign in with Odoo on the web page (web.py) and start runs
there, which open the same page at the run's private link. Hosting: connector-setup.md."""
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import anyio
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, config, jobs, pages, render, usage, usage_page, web
from .library import Library

log = logging.getLogger("icon-connector")
log.setLevel(logging.INFO)   # handshakes (_log_handshake) reach Render's log through the handler the MCP server sets up
MCP_PATH = "/mcp"
RUN_FILE = "run.json"
ATTACHED = "attached"   # the run's folder for the deck attached in the chat (deck files all end in .pptx)
RUN_ID = re.compile(r"[0-9a-f]{12}")
SLIDES = re.compile(r"\s*\d+(\s*,\s*\d+)*\s*")
LABEL = 80   # characters of an item's text shown as its label
FAILED = ("this file couldn't be read as a PowerPoint deck. Save it again from PowerPoint as a .pptx and start a "
          "new run; if it fails again, the tool's owner can find the details in the server log")

INSTRUCTIONS = """Prezlab Icons places icons from Prezlab's approved icon library into a PowerPoint deck and returns a
new copy; the designer's original file is never changed. The deck goes straight to the service from the Prezlab Icons
page, never through this chat.

1. Call start_icon_run (mode "replace" unless the designer asks for something else). In Claude web, desktop and
   mobile it opens the Prezlab Icons panel in the conversation: the designer runs the deck there and does the rest
   in it (the picks, icons for flagged placeholders, the download). Where no panel shows (for example the PowerPoint
   add-in or Claude Code), give them the run's link instead: it opens the same page in the browser. In PowerPoint,
   ask them to save the deck first and upload the saved .pptx.
2. If they attached the deck to their message, pass its path in your code tools as attached_deck, and run the
   command the result gives you straight away: the deck then appears in the panel by itself.
3. The panel tells you when they run the deck or add icons. Call get_icon_run for the details when they ask, or to
   follow a run they do at the link.
4. If they'd rather choose in the chat: offer each flagged placeholder's options by what the icon draws, then call
   choose_icons with their picks. Use search_icons when none fit. Use only icon ids these tools return. One icon
   stands for one label in a deck: don't offer an icon already used for a different label, and if the designer
   picks one anyway, say so before adding it (choose_icons also returns these as warnings).
5. Filled icons are used only with the designer's explicit okay. Set allow_filled (start_icon_run, choose_icons) or
   include_filled (search_icons) only after the designer has said in this conversation that filled icons are fine,
   never on your own judgment; if a filled icon would fit, ask first. In the panel, they tick "Allow filled icons".

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


IDLE_S = config.RUN_IDLE_HOURS * 3600
IDLE = f"{config.RUN_IDLE_HOURS:g} hour{'' if config.RUN_IDLE_HOURS == 1 else 's'}"


def _touch(run_dir: Path, run: dict) -> dict:
    """The run is in use: it lasts another RUN_IDLE_HOURS from now. Written at most once a minute, not per icon."""
    now = time.time()
    return _update(run_dir, expires=now + IDLE_S) if run["expires"] < now + IDLE_S - 60 else run


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


def _run(run_id: str) -> tuple[Path, dict]:
    """A live run. Only the conversation that started it has its id."""
    run_dir = config.WORK_DIR / run_id
    if RUN_ID.fullmatch(run_id) and (run_dir / RUN_FILE).exists():
        with _lock:
            run = _read(run_dir)
            if run["expires"] > time.time() and not run.get("ended"):
                return run_dir, _touch(run_dir, run)
    raise ToolError(f"no run '{run_id}' (a run and its files are deleted after {IDLE} unused): "
                    "start a new one with start_icon_run")


def _cleanup():
    """Delete runs unused for RUN_IDLE_HOURS, and runs the designer left while they were still placing icons (_end):
    their decks, copies, slide pictures and links go with them."""
    now = time.time()
    for path in config.WORK_DIR.glob(f"*/{RUN_FILE}"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            gone = run["expires"] < now or (run.get("ended") and run["status"] != "running")
        except (OSError, ValueError, KeyError):
            continue
        if gone:
            shutil.rmtree(path.parent, ignore_errors=True)


_sweeping = threading.Event()


def _sweep():
    """Clean up every few minutes too, so client decks don't outstay their run on a quiet server."""
    while True:
        try:
            _cleanup()
        except OSError:
            log.exception("cleaning up expired runs failed")
        time.sleep(300)


def _record(run_dir: Path, event: str, **data) -> None:
    """A usage event for this run (usage.py), with where it started: Claude and its app, or the web page and who."""
    try:
        run = _read(run_dir)
    except (OSError, ValueError):
        run = {"id": run_dir.name}
    usage.record(event, run, **data)


def _process(run_dir: Path, deck_file: str, mode: str, slides: list[int] | None, allow_filled: bool = False) -> dict:
    """Run the deck (in a worker thread; PARALLEL_RUNS at a time, the rest wait their turn) and record how it went,
    in the run and in usage (counts and Claude's tokens only)."""
    try:
        with _slots, usage.counting_tokens() as tokens:
            began, outcome = time.monotonic(), {"ok": False}
            try:
                result = jobs.run(run_dir, deck_file, _library(), mode, slides, allow_filled)
                outcome = {"ok": True, **usage.deck_run(result)}
            except (ValueError, NotImplementedError) as e:
                outcome["failure"] = "refused"
                _update(run_dir, status="failed", error=str(e))
                raise jobs.RunError(str(e))
            except Exception:   # a file python-pptx can't open, or a bug: the designer gets a reason, the log the details
                outcome["failure"] = "unreadable"
                log.exception("run %s failed", run_dir.name)
                _update(run_dir, status="failed", error=FAILED)
                raise jobs.RunError(FAILED[0].upper() + FAILED[1:] + ".", 422)
            finally:
                _record(run_dir, "deck_run", mode=mode, seconds=round(time.monotonic() - began, 1),
                        matching="claude" if config.USE_LLM else "keywords", requests=tokens.requests,
                        **tokens.counts, **outcome)
        _update(run_dir, status="done", copy_icons=None if mode == "suggest" else len(result["picks"]))
        return result
    finally:
        with _lock:   # the designer left while it ran (_end): it goes now that it's done
            try:
                ended = _read(run_dir).get("ended")
            except OSError:
                ended = False
            if ended:
                shutil.rmtree(run_dir, ignore_errors=True)


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LABEL else text[:LABEL - 1] + "…"


def _results(run_dir: Path, run: dict) -> dict:
    """A finished run in short: the icons placed (or suggested), the flagged items with the closest icons for each
    placeholder a designer can fill, the report's notes and the copy's download link."""
    rep, lib = jobs.saved(run_dir), _library()
    settings = rep["settings"]
    icons = [{"pick": n, "slide": p["item"]["slide"], "label": _label(p["item"]["text"]),
              "read_as": p["item"].get("reads_as", ""), "icon": p["icon"]["id"], "draws": p["icon"]["description"],
              "color": f"{p['color_slot']} #{p['color_hex']}", "why": p["reason"]}
             for n, p in enumerate(rep["picks"])]
    flagged = [{"flag": n, "slide": f["item"]["slide"], "label": _label(f["item"]["text"]),
                "read_as": f["item"].get("reads_as", ""), "reason": f["reason"], "can_choose": bool(f.get("spot")),
                "options": [{"icon": i, "draws": icon.description} for i in f.get("options", [])
                            if (icon := lib.get(i))]}
               for n, f in enumerate(rep["flags"])]
    has_copy = jobs.output(run_dir) is not None
    return {"run_id": run["id"], "status": "done", "mode": run["mode"], "style": settings["style"],
            "filled_icons_allowed": bool(settings.get("allow_filled")),
            "deck_color": f"{settings['color_slot']} #{settings['color_hex']}",
            "suggested" if run["mode"] == "suggest" else "placed": icons, "flagged": flagged, "notes": rep["gaps"],
            "copy": {"download": _link(run, "jobs", run["id"], "download"), "icons": run["copy_icons"]}
            if has_copy else None,
            "next": "Report the results briefly. The designer can choose icons for flagged placeholders and download "
                    "the copy in the Prezlab Icons panel (or at the link); if they'd rather choose here, offer the "
                    "options of the placeholders that can_choose and call choose_icons with their picks" + (
                        "; in suggest mode, ask which suggestions to add." if run["mode"] == "suggest" else ".")}


# --- tools ------------------------------------------------------------------------------------------------------

def _deck_name(filename: str) -> str:
    """A deck's file name on the server: its own name, made safe."""
    return (re.sub(r"[^\w\- ]+", "_", Path(filename).stem).strip(" _")[:80] or "deck") + ".pptx"


def _send_command(path: str, url: str) -> str:
    """The command that sends a deck attached in the chat from Claude's code tools to its run (_attach). The path is
    double-quoted for curl's -F, where commas and semicolons separate files and options, then quoted for the shell."""
    quoted = '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"curl -sS --fail-with-body -F {shlex.quote('deck=@' + quoted)} {shlex.quote(url)}"


def _new_run(mode: str = "replace", slides: str = "", attached_deck: str = "", allow_filled: bool = False,
             source: str = "claude", app: str = "", person: str = "") -> dict:
    """A new run, waiting for its deck. source: who started it, Claude (start_icon_run) or the web page (web.py);
    app: the Claude app, for usage; person: who signed in on the web page (Claude runs have no sign-in)."""
    _cleanup()
    run_id = uuid.uuid4().hex[:12]
    run_dir = config.WORK_DIR / run_id
    run_dir.mkdir(parents=True)
    now = time.time()
    # attached: the deck Claude sends from the chat, by the name it will arrive with; ready once it has (_attach)
    run = _write(run_dir, {
        "id": run_id, "token": secrets.token_urlsafe(24), "source": source, "app": app, "person": person,
        "mode": mode, "allow_filled": allow_filled, "status": "waiting", "error": "",
        "slides": [int(s) for s in slides.split(",") if s.strip()] or None, "deck_file": "", "copy_icons": None,
        "attached": {"name": _deck_name(re.split(r"[\\/]", attached_deck)[-1]), "ready": False}
        if attached_deck else None,
        "created": now, "expires": now + IDLE_S})
    usage.record("run_started", run, attached=bool(attached_deck))
    return run


def _start_web_run(email: str) -> str:
    """The web page's Start (web.py), for the person signed in there: a new run in replace mode, filled icons off (the
    designer can change both on the page). Returns its private link."""
    run = _new_run(source="web", app="Web page", person=email.lower())
    log.info("run %s started from the web page by %s", run["id"], email)
    return _link(run)


async def _home(request: Request) -> Response:
    return RedirectResponse(web.PATH, 303)


def start_icon_run(mode: Literal["replace", "add", "suggest"] = "replace", slides: str = "",
                   attached_deck: str = "", allow_filled: bool = False) -> CallToolResult:
    """Open Prezlab Icons for a PowerPoint deck: it places icons from Prezlab's approved library and returns a new
    copy. In Claude web, desktop and mobile this shows the Prezlab Icons panel in the conversation, where the designer
    runs the deck, reviews the picks, chooses icons for flagged placeholders and downloads the copy. Also returns
    the run's private link, which opens the same page in the browser, for clients that show no panel.

    mode: "replace" (default) swaps the PL logo placeholders designers left for icons, and changes nothing else;
    "add" also finds spots for icons on slides without placeholders; "suggest" plans icons without adding any (the
    designer then adds the ones they pick). The designer can change it in the panel.
    slides: only these slide numbers, comma-separated (e.g. "2,3,5"); empty for the whole deck.
    attached_deck: when the designer attached the .pptx to their message, its path in your code tools (e.g.
    /mnt/user-data/uploads/Client_deck.pptx). The result gives you a command to run straight after: it sends the
    deck into the panel, so they don't upload it again.
    allow_filled: true only when the designer has explicitly okayed filled icons for this deck in this conversation;
    never on your own judgment. Off, filled icons are never used. It ticks "Allow filled icons" in the panel, where
    the designer can change it."""
    if slides.strip() and not SLIDES.fullmatch(slides):
        raise ToolError('slides: slide numbers separated by commas, e.g. "2,3,5", or empty for the whole deck')
    attached_deck = attached_deck.strip()
    if attached_deck and not attached_deck.lower().endswith(".pptx"):
        raise ToolError("attached_deck: the path of the attached .pptx (a .ppt must be saved as .pptx in PowerPoint "
                        "first), or empty")
    run = _new_run(mode, slides, attached_deck, allow_filled, app=usage.client_app())
    run_id, link = run["id"], _link(run)
    told = {"run_id": run_id, "status": "waiting", "link": link, "link_expires": f"after {IDLE} unused",
            "next": "The Prezlab Icons panel shows with this result in Claude web, desktop and mobile: tell the designer "
                    "in one line to upload their deck there; don't paste the link. Only where no panel shows (the "
                    "PowerPoint add-in, Claude Code), give them the link: it opens the same page in the browser."}
    if attached_deck:
        host, url = urlparse(config.PUBLIC_URL).netloc, _link(run, "deck")
        told["attached_deck"] = {"command": _send_command(attached_deck, url)}
        told["next"] = (
            "Run attached_deck.command in your code tool now, before you answer: it sends the deck they attached into "
            "the Prezlab Icons panel, which shows with this result in Claude web, desktop and mobile and picks the "
            "deck up by itself. When it prints the deck's name, tell the designer in one line to check the mode and "
            "click Run in the panel; don't paste the link. If it fails with a network or proxy error, tell them to "
            f"choose the deck in the panel instead, and that a Claude admin can allow {host} for code execution so "
            "attached decks come straight through. Only where no panel shows (the PowerPoint add-in, Claude Code), "
            "give them the link: it opens the same page in the browser, with the deck.")
    # The panel reads structuredContent for the address it works against (base). Claude reads the text in claude.ai,
    # but some Claude apps (Cowork, Claude Code) hand it structuredContent instead: both carry the instructions.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(told, indent=2))],
                          structured_content={**told, "base": link + "/", "mode": mode, "allow_filled": allow_filled})


def get_icon_run(run_id: str) -> dict:
    """Where an icon run is. waiting: the designer hasn't run a deck yet. running: call again in a few seconds.
    failed: the reason, for the designer. done: the icons placed (or suggested), the items flagged with the reason
    and, for placeholders a designer can fill, the closest icons; and the new copy's download link."""
    run_dir, run = _run(run_id)
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


async def choose_icons(run_id: str, choices: dict[str, str] | None = None, picks: list[int] | None = None,
                       allow_filled: bool = False) -> dict:
    """Make a new copy of the run's deck with the icons the designer chose, and return its download link. The
    original deck is never changed; this run's earlier copy is replaced.

    choices: flagged placeholder number ("flag" in get_icon_run) -> icon id, from that placeholder's options or
    search_icons. Only placeholders that can_choose take one.
    picks: suggestion numbers ("pick" in get_icon_run) to add, in suggest mode. In replace and add mode the icons
    already placed stay; pass picks only to keep fewer of them.
    allow_filled: true only when the designer has explicitly okayed a filled icon in this conversation; never on
    your own judgment. Off, a filled icon is refused unless the run was started with filled icons allowed."""
    run_dir, run = _run(run_id)
    if run["status"] != "done":
        raise ToolError(f"the run isn't finished ({run['status']}): call get_icon_run first")
    try:
        chosen = {int(k): v for k, v in (choices or {}).items()}
    except ValueError:
        raise ToolError('choices: flagged placeholder numbers to icon ids, e.g. {"0": "target"}')
    if picks is None:
        picks = [] if run["mode"] == "suggest" else list(range(len(jobs.saved(run_dir)["picks"])))
    try:
        result = await anyio.to_thread.run_sync(jobs.apply, run_dir, _library(), picks, chosen, allow_filled)
    except jobs.RunError as e:
        raise ToolError(str(e))
    _update(run_dir, copy_icons=result["added"])
    _record_added(run_dir, run, "chat", picks, chosen, result)
    return {"run_id": run_id, "icons_in_copy": result["added"], "download": _link(run, "jobs", run_id, "download"),
            "not_added": [f"{i}: no longer approved in the library" for i in result["skipped"]],
            "warnings": result["warnings"],
            "next": "Tell the designer the new copy is ready: Download deck in the panel gets it, or this link." + (
                " Pass on each warning in their words first: one icon should stand for one label, and these choices "
                "reuse an icon. Offer other options for those placeholders if they want to change them."
                if result["warnings"] else "")}


def search_icons(query: str, limit: int = 10, include_filled: bool = False) -> dict:
    """Search Prezlab's approved icon library by meaning, with English or Arabic keywords (e.g. "growth",
    "cyber security", "نمو"). Use it for a flagged placeholder when none of its options fit. Returns icon ids and
    what each draws, the house pick first.
    include_filled: true only when the designer has explicitly okayed filled icons in this conversation; never on
    your own judgment. Off, filled icons are left out."""
    lib = _library()
    limit = max(1, min(limit, 25))
    arabic = bool(re.search(r"[؀-ۿ]", query))
    styles = {i.style for i in lib.icons.values()} - (set() if include_filled else config.OPT_IN_STYLES)
    found = []
    if key := lib.lookup(query):
        found += [lib.concept_map[key]["id"], *lib.concept_map[key]["alts"]]
    for style in sorted(styles):
        found += [i.id for i in lib.search(query, style, arabic, limit)]
    icons = [icon for i in dict.fromkeys(found) if (icon := lib.get(i)) and icon.style in styles][:limit]
    # usage: the words only when nothing matched (a gap in the library), cut short
    usage.record("search", {"source": "claude", "app": usage.client_app()}, via="chat", results=len(icons),
                 query="" if icons else usage.clean(" ".join(query.split()), usage.QUERY_CHARS))
    return {"query": query,
            "icons": [{"icon": i.id, "style": i.style, "family": i.family, "draws": i.description,
                       "keywords": i.tags[:8]} for i in icons],
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
EXPIRED = (f"This run has ended: its deck and copy are deleted when you leave its page, or after {IDLE} unused. "
           "Ask Claude to start a new one, or start one from your Prezlab Icons web page.")
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"   # an icon opened on its own runs nothing


def _page(host: str) -> str:
    """The page with a hosting script in place of its HOST marker."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    if HOST_MARK not in page:
        raise RuntimeError(f"static/index.html has lost its {HOST_MARK} marker")
    return page.replace(HOST_MARK, host, 1)


def panel_html(origin: str = "") -> str:
    """The panel Claude shows: the page, the MCP Apps client and the bridge (panel.js), all inline, since a panel can't
    load scripts from anywhere. The client is an ES module; inside one async function, with its export statement
    turned into a name, it runs as a plain script, as Image Studio's panel does. The brand font comes from this
    server (origin), which the panel's CSP allows."""
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
    page = _page(f"<script>{host}</script>\n<script>{script}</script>").replace(pages.TAB_ICON, "", 1)   # a panel has no tab
    return page.replace('url("/fonts/', f'url("{origin}/fonts/')


def _gone() -> HTMLResponse:
    home = f'<a class="button" href="{web.PATH}">Start a new run</a>' if web.enabled() else ""
    return pages.render(f"<h1>This link has expired</h1><p>{EXPIRED}</p>{home}", 404)


def _link_run(request: Request) -> tuple[Path, dict] | None:
    """The run behind a private link, while the link is valid. Every request through it keeps the run in use."""
    run_id, token = request.path_params["run_id"], request.path_params["token"]
    run_dir = config.WORK_DIR / run_id
    with _lock:
        try:
            run = _read(run_dir) if RUN_ID.fullmatch(run_id) else None
        except OSError:   # no such run, or deleted just now
            return None
        if (run is None or not hmac.compare_digest(run["token"].encode(), token.encode())
                or run["expires"] < time.time() or run.get("ended")):
            return None
        return run_dir, _touch(run_dir, run)


async def run_page(request: Request) -> Response:
    """The page at the run's private link, for a browser (where Claude shows no panel, or to open it full size)."""
    found = _link_run(request)
    if found is None:
        return _gone()
    run = found[1]
    base, source = json.dumps(f"/runs/{run['id']}/{run['token']}/"), json.dumps(run.get("source", "claude"))
    # A web page run belongs to its page: the address bar shows the web page, so a refresh goes back there, and
    # leaving ends the run (_end). A link from Claude stays the run's address: Claude follows that run.
    home = json.dumps(web.PATH if run.get("source") == "web" and web.enabled() else None)
    host = (f'<script>window.HOST = {{mode: "page", base: {base}, source: {source}, home: {home}, '
            'ready: Promise.resolve()};</script>')
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
    """What the page opens with: the mode and slides Claude set, the deck attached in the chat (the page asks again
    until it's ready) and the run's latest result."""
    result = _for_page(run, jobs.saved(run_dir)) if run["status"] == "done" else None
    return JSONResponse({"status": run["status"], "error": run["error"], "mode": run["mode"],
                         "slides": ",".join(map(str, run["slides"] or [])),
                         "allow_filled": bool(run.get("allow_filled")), "attached": run.get("attached"),
                         "result": result})


async def _library_index(request: Request, run_dir: Path, run: dict) -> Response:
    return JSONResponse(jobs.library_index(_library()))


async def _icon(request: Request, run_dir: Path, run: dict) -> Response:
    lib = _library()
    icon = lib.all.get(request.path_params["icon_id"])
    if icon is None:
        raise jobs.RunError("unknown icon", 404)
    return _svg(jobs.icon_svg(lib.svg_bytes(icon), request.query_params.get("color", "")))


def _clear(run_dir: Path):
    """Everything from the run's last deck (the deck, its report and copy, uploaded icons, slide pictures). The deck
    attached in the chat stays, for the next Run."""
    for path in run_dir.iterdir():
        if not path.name.startswith(RUN_FILE) and path.name != ATTACHED:
            shutil.rmtree(path) if path.is_dir() else path.unlink()


async def _check_deck(deck) -> None:
    if not isinstance(deck, UploadFile) or not (deck.filename or "").lower().endswith(".pptx"):
        raise jobs.RunError("Choose a PowerPoint deck (.pptx).")
    if await deck.read(4) != b"PK\x03\x04":   # every .pptx is a zip
        raise jobs.RunError("This file isn't a PowerPoint deck: save it from PowerPoint as a .pptx.")
    await deck.seek(0)


def _save_deck(deck: UploadFile, path: Path):
    with path.open("wb") as f:
        shutil.copyfileobj(deck.file, f)


async def _attach(request: Request, run_dir: Path, run: dict) -> Response:
    """The deck the designer attached to their message, sent by Claude from its code tools (start_icon_run's
    attached_deck). It waits with the run, and the page picks it up in place of choosing a file."""
    form = await request.form(max_files=1, max_fields=0)
    deck = form.get("deck")
    await _check_deck(deck)
    name, folder = _deck_name(deck.filename), run_dir / ATTACHED
    shutil.rmtree(folder, ignore_errors=True)
    try:
        folder.mkdir()
        _save_deck(deck, folder / name)
    except (OSError, jobs.RunError) as e:
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(e, jobs.RunError):
            raise
        log.exception("run %s: saving the attached deck failed", run["id"])
        raise jobs.RunError("The deck didn't save. Try again.", 500)
    _update(run_dir, attached={"name": name, "ready": True})
    _record(run_dir, "deck_attached")
    return JSONResponse({"deck": name, "next": "The deck is in the Prezlab Icons panel (and at the run's link): "
                                               "tell the designer in one line to check the mode and click Run there."})


async def _run_deck(request: Request, run_dir: Path, run: dict) -> Response:
    """The page's Run: the deck chosen in the page, else the one attached in the chat, is saved, run and its report
    returned, as the test console's POST /jobs does. Running again starts the run over with the new deck."""
    form = await request.form(max_files=1, max_fields=4)
    deck, mode, slides = form.get("deck"), str(form.get("mode") or run["mode"]), str(form.get("slides") or "")
    # "Allow filled icons": the designer's tick in the page (Claude's allow_filled only pre-ticks it); off if absent
    allow_filled = str(form.get("allow_filled") or "").lower() in {"on", "true", "1"}
    chosen = isinstance(deck, UploadFile) and bool(deck.filename)   # an empty file field sends a nameless file
    attached = run.get("attached") or {}
    if chosen:
        await _check_deck(deck)
    elif not (attached.get("ready") and (run_dir / ATTACHED / attached["name"]).is_file()):
        raise jobs.RunError("Choose a PowerPoint deck (.pptx).")
    if mode not in MODES:
        raise jobs.RunError(f"Mode: one of {', '.join(MODES)}.")
    if slides.strip() and not SLIDES.fullmatch(slides):
        raise jobs.RunError("Slides: slide numbers separated by commas, e.g. 2,3,5, or empty for the whole deck.")
    if not _claim(run_dir):
        raise jobs.RunError("This run is still placing icons: wait for it to finish.", 409)
    try:
        name = _deck_name(deck.filename) if chosen else attached["name"]
        _clear(run_dir)
        if chosen:
            _save_deck(deck, run_dir / name)
        else:
            shutil.copyfile(run_dir / ATTACHED / name, run_dir / name)
    except (OSError, jobs.RunError) as e:
        _clear(run_dir)
        _update(run_dir, status="failed", error=str(e) if isinstance(e, jobs.RunError) else "the upload didn't save")
        if isinstance(e, jobs.RunError):
            raise
        log.exception("run %s: saving the upload failed", run["id"])
        raise jobs.RunError("The upload didn't save. Try again.", 500)
    slide_list = [int(n) for n in slides.split(",") if n.strip()] or None
    _update(run_dir, mode=mode, slides=slide_list, deck_file=name, allow_filled=allow_filled)
    result = await anyio.to_thread.run_sync(_process, run_dir, name, mode, slide_list, allow_filled)
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
    _record_added(run_dir, run, "page", picks, choices, result)
    return JSONResponse({**result, "download": f"jobs/{run['id']}/download"})


def _record_added(run_dir: Path, run: dict, via: str, picks: list[int], choices: dict[int, str], result: dict):
    """Usage: a copy made with the designer's picks and choices, in the page or from the chat. The icons are the
    library ones the designer chose (their own uploads only counted), and in suggest mode the suggestions they added."""
    from_library = [v for v in choices.values() if jobs.upload_path(run_dir, v) is None]
    icons = list(from_library)
    if run["mode"] == "suggest":
        offered = jobs.saved(run_dir)["picks"]
        icons += [offered[n]["icon"]["id"] for n in sorted(set(picks)) if 0 <= n < len(offered)]
    _record(run_dir, "icons_added", via=via, picks=len(set(picks)), chosen=len(choices),
            uploads=len(choices) - len(from_library), warnings=len(result["warnings"]), in_copy=result["added"],
            icons=icons)


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


async def _alive(request: Request, run_dir: Path, run: dict) -> Response:
    """The page is still open (it says so every few minutes): _link_run has kept the run in use."""
    return Response(status_code=204)


async def _end(request: Request, run_dir: Path, run: dict) -> Response:
    """The designer left a web page run (closed it, refreshed, went elsewhere): its deck, copy and slide pictures are
    deleted now, or once it's done if it's still placing icons. Runs Claude started stay until they've been unused for
    RUN_IDLE_HOURS: Claude follows those, and their link may be opened again."""
    if run.get("source") == "web":
        with _lock:
            if _read(run_dir)["status"] == "running":
                _update(run_dir, ended=True)
            else:
                shutil.rmtree(run_dir, ignore_errors=True)
    return Response(status_code=204)


async def _download(request: Request, run_dir: Path, run: dict) -> Response:
    out = jobs.output(run_dir)
    if out is None:
        raise jobs.RunError("There's no copy yet: add icons first.", 404)
    _record(run_dir, "download")
    return FileResponse(out, filename=out.name)


RUN_LINK = "/runs/{run_id}/{token}"
API = [("/state", ["GET"], _state), ("/alive", ["POST"], _alive), ("/end", ["POST"], _end), ("/library", ["GET"], _library_index), ("/icons/{icon_id}.svg", ["GET"], _icon),
       ("/deck", ["POST"], _attach), ("/jobs", ["POST"], _run_deck), ("/jobs/{job}/apply", ["POST"], _apply),
       ("/jobs/{job}/uploads", ["POST"], _upload_icon), ("/jobs/{job}/uploads/{digest}.svg", ["GET"], _uploaded_icon),
       ("/jobs/{job}/slides/{n:int}.png", ["GET"], _slide), ("/jobs/{job}/download", ["GET"], _download)]


FONTS = STATIC / "fonts"


async def font(request: Request) -> Response:
    """The brand font (static/fonts), for the page, the sign-in pages and Claude's panel, which loads it from another
    origin: fonts need CORS for that."""
    name = request.path_params["name"]
    path = FONTS / name
    if not (re.fullmatch(r"[\w-]+\.woff2", name) and path.is_file()):
        return Response(status_code=404)
    return FileResponse(path, media_type="font/woff2", headers={
        "Access-Control-Allow-Origin": "*", "Cross-Origin-Resource-Policy": "cross-origin",
        "Cache-Control": "public, max-age=86400"})


TAB_ICON = STATIC / "brand" / "prezlab-icon.png"


async def tab_icon(request: Request) -> Response:
    """The "pl:" mark in the browser tab, for the run page and the sign-in pages (static/brand/README.md)."""
    return FileResponse(TAB_ICON, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def health(request: Request) -> Response:
    """Up, how many icons, and whether slide pictures can be made here (LibreOffice in the Docker image)."""
    return JSONResponse({"ok": True, "icons": len(_library().icons), "slide_pictures": render.available()})


# --- app --------------------------------------------------------------------------------------------------------

UI_EXTENSION = {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}


def _declare_ui(body: bytes) -> bytes:
    """An initialize result with the MCP Apps extension in its capabilities; any other response as it was."""
    try:
        message = json.loads(body)
    except ValueError:
        return body
    result = message.get("result") if isinstance(message, dict) else None
    if not (isinstance(result, dict) and "protocolVersion" in result and isinstance(result.get("capabilities"), dict)):
        return body
    caps = result["capabilities"]
    caps["extensions"] = {**(caps.get("extensions") or {}), **UI_EXTENSION}
    return json.dumps(message, separators=(",", ":")).encode()


def _log_handshake(body: bytes, headers: dict) -> None:
    """One line in the server log per client that connects: the app it says it is, the extensions it says it
    supports, and so whether it can show the Prezlab Icons panel (the MCP Apps extension). This is how to tell
    whether a Claude app (the PowerPoint add-in) can show the panel at all. Names, versions and flags only: nothing
    from a conversation, and never the connector's address (its secret)."""
    try:
        message = json.loads(body)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("method") != "initialize":
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    caps = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    extensions = caps.get("extensions") if isinstance(caps.get("extensions"), dict) else {}
    clean = lambda v: re.sub(r"[^\w .:/()+;,=-]", "?", str(v))[:120]   # one plain line, whatever a client sends
    ui = extensions.get(next(iter(UI_EXTENSION)))
    types = ui.get("mimeTypes") if isinstance(ui, dict) and isinstance(ui.get("mimeTypes"), list) else []
    agent = headers.get(b"user-agent", b"?").decode("latin-1")
    log.info("client connected: %s %s, protocol %s, extensions: %s, can show the panel: %s, user agent: %s",
             clean(client.get("name", "?")), clean(client.get("version", "?")), clean(params.get("protocolVersion", "?")),
             clean(", ".join(map(str, extensions)) or "none"),
             f"yes ({clean(', '.join(map(str, types)))})" if ui is not None else "no", clean(agent))
    usage.saw_client(client.get("name", "?"), agent, ui is not None)


CLIENT_INFO = "io.modelcontextprotocol/clientInfo"   # newer protocol versions: the client's name in each request's _meta


def _client_name(body: bytes) -> str:
    """The Claude app's own name in an MCP request: the handshake's clientInfo, or on newer protocol versions the
    request's _meta. Empty when the request doesn't say (tool calls on the handshake versions)."""
    try:
        message = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(message, dict):   # a batch: no name to go by
        return ""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    info = params.get("clientInfo") if message.get("method") == "initialize" else meta.get(CLIENT_INFO)
    return usage.clean(info.get("name"), 60) if isinstance(info, dict) else ""


class DeclareApps:
    """Claude mounts a panel only for a server that declares the MCP Apps extension when it connects. The SDK keeps
    capabilities.extensions for the newest protocol and sieves it out on the handshake versions Claude connects with,
    so the declaration goes back into the initialize result here (JSON responses: json_response=True). The request
    side of each handshake is logged (_log_handshake), and every request carries its Claude app to the tools, for
    usage (usage.mcp_request)."""

    def __init__(self, app, path: str):
        self.app, self.path = app, path.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return
        start, chunks, request = None, [], []
        headers = dict(scope.get("headers") or [])

        async def read():
            message = await receive()
            if message["type"] == "http.request":
                request.append(message.get("body", b""))
                if not message.get("more_body"):
                    body = b"".join(request)
                    _log_handshake(body, headers)
                    holder["client"] = _client_name(body)
            return message

        async def declare(message):
            nonlocal start
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or [])
                if not headers.get(b"content-type", b"").startswith(b"application/json"):
                    await send(message)   # not a JSON result: passed on as it is
                    return
                start = message
            elif message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    body = _declare_ui(b"".join(chunks))
                    headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                    await send({**start, "headers": [*headers, (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
            else:
                await send(message)

        with usage.mcp_request(headers.get(b"user-agent", b"").decode("latin-1")) as holder:
            await self.app(scope, read, declare)


def build_app(mode: str | None = None):
    """The connector: the MCP endpoint (at /mcp, or /mcp/<secret>: auth.py) with its tools and the Prezlab Icons
    panel, and the page and its endpoints under each run's private link."""
    mode = mode or auth.mode()
    url = urlparse(config.PUBLIC_URL)
    origin = f"{url.scheme}://{url.netloc}"
    apps = Apps()   # the panel: start_icon_run shows it; it may reach this server only (CSP), for data and icons
    apps.add_html_resource(PANEL_URI, panel_html(origin), name="Prezlab Icons",
                           description="Run a deck, review the picks, choose icons for flagged placeholders, download.",
                           csp=ResourceCsp(connect_domains=[origin], resource_domains=[origin]))
    fn, annotations = PANEL_TOOL
    apps.tool(resource_uri=PANEL_URI, annotations=annotations)(fn)
    server = MCPServer("prezlab-icons", title="Prezlab Icons", instructions=INSTRUCTIONS, extensions=[apps])
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations)
    server.custom_route(RUN_LINK, ["GET"])(run_page)
    for path, methods, handler in API:
        server.custom_route(RUN_LINK + path, [*methods, "OPTIONS"])(_api(handler))
    server.custom_route("/health", ["GET"])(health)
    server.custom_route("/fonts/{name}", ["GET"])(font)
    server.custom_route("/favicon.png", ["GET"])(tab_icon)
    if web.enabled():   # the web page for people without Claude, behind Odoo sign-in; the service's address leads there
        web.check()
        server.custom_route(web.PATH, ["GET", "POST"])(web.endpoint(_start_web_run))
        server.custom_route("/", ["GET"])(_home)
        if usage_page.enabled():   # usage, for the tool's owners (ICON_USAGE_ADMINS), signed in on the web page
            server.custom_route(web.USAGE_PATH, ["GET"])(usage_page.page)
            server.custom_route(web.USAGE_PATH + ".csv", ["GET"])(usage_page.export)
    local =["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    security = TransportSecuritySettings(   # Host and Origin checks against DNS rebinding
        enable_dns_rebinding_protection=True, allowed_hosts=[url.netloc, *local],
        allowed_origins=[f"{url.scheme}://{url.netloc}", "https://claude.ai", "https://pivot.claude.ai",
                         "http://127.0.0.1:*", "http://localhost:*"])
    path = auth.mcp_path(mode, MCP_PATH)
    app = DeclareApps(server.streamable_http_app(streamable_http_path=path, stateless_http=True, json_response=True,
                                                 transport_security=security), path)
    if not _sweeping.is_set():
        _sweeping.set()
        threading.Thread(target=_sweep, daemon=True, name="run-cleanup").start()
    return auth.RequireToken(app, MCP_PATH, config.MCP_TOKEN) if mode == "token" else app


app = build_app()
