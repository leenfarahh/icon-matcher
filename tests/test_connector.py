"""The Claude connector (app/connector.py, app/auth.py) and the test UI's runs (app/main.py, app/jobs.py), driven over
HTTP the way Claude and a designer's browser use them. Offline: no Claude API calls."""
import json
import shlex
import time

import httpx2
import pytest
from pptx import Presentation
from pptx.util import Inches
from starlette.testclient import TestClient

from app import auth, config, connector, odoo, pages, web
from app.library import Library
from test_pipeline import make_deck, make_library, pl_logo

BASE = "http://127.0.0.1:8001"
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
       "MCP-Protocol-Version": "2025-06-18"}
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Keyword matching only, runs and the library in tmp_path, the connector on this machine."""
    monkeypatch.setattr(config, "USE_LLM", False)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "lib")
    monkeypatch.setattr(config, "PUBLIC_URL", BASE)
    for name in ("ODOO_URL", "ODOO_DB"):   # the web page is off unless a test sets it up (fake_odoo), whatever .env says
        monkeypatch.setattr(config, name, "")
    make_library(tmp_path / "lib")
    connector._lib.clear()
    yield tmp_path
    connector._lib.clear()


def client(mode="none"):
    return TestClient(connector.build_app(mode), base_url=BASE)


def call(c, tool, headers=None, **arguments):
    """One tool call over MCP. Returns (is_error, the result: parsed JSON, or the error's message)."""
    r = c.post("/mcp", headers={**MCP, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return True, text.removeprefix(f"Error executing tool {tool}: ")
    return False, json.loads(text)


PANEL = "https://3f1c0a9e2b7d4e6f8a1b2c3d4e5f6a7b.claudemcpcontent.com"   # where Claude runs the panel


def start(c, headers=None, **arguments):
    """start_icon_run as Claude calls it: (what Claude reads, what the panel reads)."""
    r = c.post("/mcp", headers={**MCP, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "start_icon_run", "arguments": arguments}})
    result = r.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"]), result["structuredContent"]


def run_deck(c, base, path, name="Client deck.pptx", **form):
    """The page's Run, from the panel: the deck goes straight to the run's endpoint."""
    with open(path, "rb") as f:
        return c.post(base + "jobs", files={"deck": (name, f, PPTX)}, data=form, headers={"Origin": PANEL})


def rpc(c, method, params=None):
    return c.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                            "params": params or {}}).json()["result"]


def flagged_deck(path):
    """Two PL placeholders on one slide: the small library has an icon for one label but not the other, so the
    slide's set is flagged whole and a designer chooses."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((1, "Revenue growth"), (5, "Quarterly outlook")):
        pl_logo(slide.shapes, x + 1, 2.0, 0.6, name="PL logo")
        slide.shapes.add_textbox(Inches(x), Inches(2.9), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(path)


def icons_in(path) -> list[str]:
    return [s.name for slide in Presentation(path).slides for s in slide.shapes if s.name.startswith("Icon - ")]


# --- the page, in Claude's panel or at the run's private link -------------------------------------------------

def test_start_icon_run_opens_the_panel_and_gives_claude_the_link_for_clients_without_one(env):
    with client() as c:
        tool = next(t for t in rpc(c, "tools/list")["tools"] if t["name"] == "start_icon_run")
        assert tool["_meta"]["ui"]["resourceUri"] == connector.PANEL_URI
        [resource] = rpc(c, "resources/list")["resources"]
        assert resource["uri"] == connector.PANEL_URI and resource["mimeType"] == "text/html;profile=mcp-app"
        assert resource["_meta"]["ui"]["csp"] == {"connectDomains": [BASE], "resourceDomains": [BASE]}
        page = rpc(c, "resources/read", {"uri": connector.PANEL_URI})["contents"][0]["text"]
        assert 'mode: "panel"' in page and "const App = " in page and "new App(" in page   # client and bridge inline
        assert connector.HOST_MARK not in page and "export{" not in page.replace(" ", "")
        assert page.count("</script>") == page.count("<script")                            # nothing ends a tag early

        told, panel = start(c, mode="suggest", slides="2,3")
        assert told["link"] == panel["link"] and panel["base"] == panel["link"] + "/"
        assert panel["link"].startswith(f"{BASE}/runs/{told['run_id']}/") and panel["mode"] == "suggest"
        assert "panel" in told["next"] and "link" in told["next"]
        state = c.get(panel["base"] + "state", headers={"Origin": PANEL})
        assert state.json() == {"status": "waiting", "error": "", "mode": "suggest", "slides": "2,3",
                                "allow_filled": False, "attached": None, "result": None}
        assert "attached_deck" not in told
        assert state.headers["access-control-allow-origin"] == PANEL

        browser = c.get(panel["link"])                                  # the same page, in a browser
        assert browser.status_code == 200 and f'base: "/runs/{told["run_id"]}/' in browser.text
        assert 'mode: "page"' in browser.text and "Run on a deck" in browser.text
        assert browser.headers["x-frame-options"] == "DENY" and browser.headers["referrer-policy"] == "no-referrer"


def test_a_deck_run_in_the_page_comes_back_as_a_copy_with_icons(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        told, panel = start(c, mode="add")
        r = run_deck(c, panel["base"], deck, mode="add")
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == PANEL
        result = r.json()
        assert result["job"] == told["run_id"] and result["output_file"] == "Client deck_icons.pptx"
        assert [p["icon"]["id"] for p in result["picks"]] == ["finance-growth-chart", "people-team", "risk-shield"]

        done = call(c, "get_icon_run", run_id=told["run_id"])[1]                # Claude sees the same run
        assert done["status"] == "done" and done["copy"]["icons"] == 3
        assert done["copy"]["download"] == panel["base"] + f"jobs/{told['run_id']}/download"
        copy = c.get(done["copy"]["download"])
        assert copy.status_code == 200 and "Client%20deck_icons.pptx" in copy.headers["content-disposition"]
        (env / "copy.pptx").write_bytes(copy.content)
        assert len(icons_in(env / "copy.pptx")) == 3
        assert icons_in(env / "jobs" / told["run_id"] / "Client deck.pptx") == []    # the upload is never changed
        state = c.get(panel["base"] + "state").json()["result"]
        assert state["picks"][0]["icon"]["id"] == "finance-growth-chart" and state["output_file"] == "Client deck_icons.pptx"

        make_deck(deck, ["Team expansion", "Risk management"])                       # Run again: a fresh start
        again = run_deck(c, panel["base"], deck, name="Second.pptx", mode="add").json()
        assert [p["icon"]["id"] for p in again["picks"]] == ["people-team", "risk-shield"]
        assert sorted(f.name for f in (env / "jobs" / told["run_id"]).iterdir()) == \
            ["Second.pptx", "Second_icons.pptx", "report.json", "run.json"]


NO_FILE = {"deck": ("", b"", "application/octet-stream")}   # what the page's form sends when no file is chosen


def test_the_panel_opens_first_and_picks_up_the_deck_attached_in_the_chat(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        told, panel = start(c, mode="add", attached_deck="/mnt/user-data/uploads/Client deck.pptx")   # the panel
        url = panel["base"] + "deck"
        assert shlex.split(told["attached_deck"]["command"]) == [
            "curl", "-sS", "--fail-with-body", "-F", 'deck=@"/mnt/user-data/uploads/Client deck.pptx"', url]
        assert "attached_deck.command" in told["next"] and "before you answer" in told["next"]
        assert {k: panel[k] for k in told} == told      # also for Claude apps that read structuredContent, not text
        state = lambda: c.get(panel["base"] + "state", headers={"Origin": PANEL}).json()
        assert state()["attached"] == {"name": "Client deck.pptx", "ready": False}      # the panel waits for it
        no_deck = c.post(panel["base"] + "jobs", files=NO_FILE, data={"mode": "add"}, headers={"Origin": PANEL})
        assert no_deck.json()["detail"] == "Choose a PowerPoint deck (.pptx)."

        with open(deck, "rb") as f:                                        # Claude's curl, from its code tools
            arrived = c.post(url, files={"deck": ("Client deck.pptx", f, PPTX)})
        assert arrived.status_code == 200 and arrived.json()["deck"] == "Client deck.pptx"
        assert "click Run" in arrived.json()["next"]
        assert state()["attached"] == {"name": "Client deck.pptx", "ready": True} and state()["status"] == "waiting"

        for _ in range(2):                                                 # Run, and Run again: it stays
            r = c.post(panel["base"] + "jobs", files=NO_FILE, data={"mode": "add"}, headers={"Origin": PANEL})
            assert r.status_code == 200 and r.json()["output_file"] == "Client deck_icons.pptx"
            assert [p["icon"]["id"] for p in r.json()["picks"]] == ["finance-growth-chart", "people-team", "risk-shield"]
        assert icons_in(env / "jobs" / told["run_id"] / "attached" / "Client deck.pptx") == []

        make_deck(deck, ["Team expansion", "Risk management"])             # a file chosen in the panel still wins
        assert [p["icon"]["id"] for p in run_deck(c, panel["base"], deck, "Other.pptx", mode="add").json()["picks"]] \
            == ["people-team", "risk-shield"]

        (env / "notes.txt").write_text("not a deck")
        with open(env / "notes.txt", "rb") as f:
            assert c.post(url, files={"deck": ("notes.pptx", f, PPTX)}).status_code == 400
        with open(deck, "rb") as f:
            assert c.post(panel["link"][:-3] + "abc/deck", files={"deck": ("d.pptx", f, PPTX)}).status_code == 404
        assert call(c, "start_icon_run", attached_deck="/mnt/user-data/uploads/old.ppt")[0]


def test_claude_is_told_the_panel_extension_on_every_handshake_version(env):
    with client() as c:
        for version in ("2025-06-18", "2025-11-25"):
            init = c.post("/mcp", headers={**MCP, "MCP-Protocol-Version": version}, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": version, "clientInfo": {"name": "claude-ai", "version": "1"},
                    "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {
                        "mimeTypes": ["text/html;profile=mcp-app"]}}}}})
            result = init.json()["result"]
            assert result["protocolVersion"] == version and result["capabilities"]["tools"] is not None
            assert result["capabilities"]["extensions"] == {
                "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}
            assert int(init.headers["content-length"]) == len(init.content)
        assert "extensions" not in json.dumps(rpc(c, "tools/list"))           # only the handshake changes


def test_each_client_that_connects_is_logged_with_whether_it_can_show_the_panel(env, monkeypatch, caplog):
    """How to tell whether a Claude app (the PowerPoint add-in) can show the panel: its handshake, in the log."""
    monkeypatch.setattr(config, "MCP_SECRET", SECRET)
    caplog.set_level("INFO", logger="icon-connector")
    with client("secret") as c:
        for name, extensions in (("claude-ai", connector.UI_EXTENSION), ("office\nadd-in", {})):
            c.post(f"/mcp/{SECRET}", headers={**MCP, "User-Agent": "Claude-User"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "clientInfo": {"name": name, "version": "1.2"},
                    "capabilities": {"extensions": extensions}}})
        c.post(f"/mcp/{SECRET}", headers=MCP, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    lines = [r.getMessage() for r in caplog.records if r.name == "icon-connector"]
    assert lines == [
        "client connected: claude-ai 1.2, protocol 2025-06-18, extensions: io.modelcontextprotocol/ui, "
        "can show the panel: yes (text/html;profile=mcp-app), user agent: Claude-User",
        "client connected: office?add-in 1.2, protocol 2025-06-18, extensions: none, can show the panel: no, "
        "user agent: Claude-User"]                    # one plain line each, and only handshakes are logged
    assert SECRET not in caplog.text                   # never the connector's secret address


def test_an_attached_decks_path_reaches_curl_whole():
    path = '/mnt/user-data/uploads/Q3, final; v2 "client".pptx'      # commas and semicolons separate curl -F parts
    argv = shlex.split(connector._send_command(path, "https://x.onrender.com/runs/a/b/deck"))
    assert argv[4] == 'deck=@"/mnt/user-data/uploads/Q3, final; v2 \\"client\\".pptx"'
    assert connector._deck_name(path.rsplit("/", 1)[1]) == "Q3_ final_ v2 _client.pptx"


def test_suggestions_go_into_the_copy_only_once_chosen(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        told, panel = start(c, mode="suggest")
        result = run_deck(c, panel["base"], deck, mode="suggest").json()
        assert len(result["picks"]) == 3 and result["output_file"] is None
        download = panel["base"] + f"jobs/{told['run_id']}/download"
        assert c.get(download).json()["detail"] == "There's no copy yet: add icons first."

        apply = panel["base"] + f"jobs/{told['run_id']}/apply"
        preflight = c.options(apply, headers={"Origin": PANEL, "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == PANEL
        added = c.post(apply, json={"picks": [0, 2], "choices": {}}, headers={"Origin": PANEL}).json()
        assert added["added"] == 2 and added["download"] == f"jobs/{told['run_id']}/download"
        (env / "copy.pptx").write_bytes(c.get(download).content)
        assert sorted(icons_in(env / "copy.pptx")) == ["Icon - finance-growth-chart", "Icon - risk-shield"]
        assert c.post(apply, json={"picks": [], "choices": {}}).json()["detail"] == "choose at least one icon"
        assert call(c, "choose_icons", run_id=told["run_id"], picks=[7])[1] == "unknown suggestion: run the deck again"


def test_flagged_placeholders_get_the_icons_the_designer_chose_or_uploaded(env, monkeypatch):
    monkeypatch.setattr(connector.jobs.render, "available", lambda: False)   # as on Render: no PowerPoint
    deck = env / "deck.pptx"
    flagged_deck(deck)
    with client() as c:
        told, panel = start(c)
        result = run_deck(c, panel["base"], deck).json()
        assert result["picks"] == [] and [f["item"]["text"] for f in result["flags"]] == ["Revenue growth",
                                                                                          "Quarterly outlook"]
        assert all(f["spot"] for f in result["flags"])
        job = panel["base"] + f"jobs/{told['run_id']}/"
        shot = c.get(job + "slides/1.png", headers={"Origin": PANEL})             # no PowerPoint here: a blank slide,
        assert shot.status_code == 404 and shot.json()["detail"] == \
            "slide pictures need PowerPoint or LibreOffice where the tool runs"    # and the panel can read why
        assert shot.headers["access-control-allow-origin"] == PANEL

        assert c.post(job + "apply", json={"choices": {"0": "made-up"}}).json()["detail"] == \
            "choose approved outline icons from the library, or upload one"
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M4 12h16M12 4v16"/></svg>'
        mine = c.post(job + "uploads", files={"file": ("plus.svg", svg, "image/svg+xml")}).json()
        assert mine["id"].startswith("upload:") and mine["name"] == "plus.svg"
        preview = c.get(job + f"uploads/{mine['id'][7:]}.svg?color=FD5108")
        assert preview.status_code == 200 and "default-src 'none'" in preview.headers["content-security-policy"]
        bad = c.post(job + "uploads", files={"file": ("notes.svg", "not an icon", "image/svg+xml")})
        assert bad.status_code == 400 and isinstance(bad.json()["detail"], list)

        added = c.post(job + "apply", json={"choices": {"0": "finance-growth-chart", "1": mine["id"]}}).json()
        assert added["added"] == 2
        (env / "copy.pptx").write_bytes(c.get(job + "download").content)
        shapes = [s.name for s in Presentation(env / "copy.pptx").slides[0].shapes]
        assert sum(n.startswith("Icon - ") for n in shapes) == 2 and "PL logo" not in shapes


def test_a_designer_choosing_an_icon_used_for_another_label_is_warned_in_the_panel_and_in_chat(env):
    deck = env / "deck.pptx"
    flagged_deck(deck)                                    # "Revenue growth" and "Quarterly outlook", both flagged
    with client() as c:
        told, panel = start(c)
        result = run_deck(c, panel["base"], deck).json()
        assert [f["item"]["label"] for f in result["flags"]] == ["revenue growth", "quarterly outlook"]
        added = c.post(panel["base"] + f"jobs/{told['run_id']}/apply",
                       json={"choices": {"0": "finance-growth-chart", "1": "finance-growth-chart"}}).json()
        assert added["added"] == 2 and added["warnings"] == [
            '"Quarterly outlook" (slide 1): finance-growth-chart is already used for "Revenue growth" on slide 1. '
            "Clients may read the two as the same thing, so choose another icon unless that's intended."]
        chat = call(c, "choose_icons", run_id=told["run_id"], choices={"0": "finance-growth-chart",
                                                                        "1": "finance-growth-chart"})[1]
        assert chat["warnings"] == added["warnings"] and "Pass on each warning" in chat["next"]
        clean = call(c, "choose_icons", run_id=told["run_id"], choices={"0": "finance-growth-chart",
                                                                         "1": "people-team"})[1]
        assert clean["warnings"] == [] and "warning" not in clean["next"]


def test_filled_icons_need_the_designers_okay_in_the_connector(env):
    from test_pipeline import add_filled_icons
    add_filled_icons(env / "lib")
    connector._lib.clear()
    deck = env / "deck.pptx"
    flagged_deck(deck)
    with client() as c:
        assert call(c, "search_icons", query="location")[1]["icons"] == []          # left out of a search
        shown = call(c, "search_icons", query="location", include_filled=True)[1]["icons"]
        assert [(i["icon"], i["style"]) for i in shown] == [("pin-solid", "filled")]

        told, panel = start(c)
        run_deck(c, panel["base"], deck)
        assert call(c, "choose_icons", run_id=told["run_id"], choices={"0": "pin-solid"}) == \
            (True, "pin-solid is a filled icon: filled icons are used only when the designer okays them")
        added = call(c, "choose_icons", run_id=told["run_id"], choices={"0": "pin-solid"}, allow_filled=True)[1]
        assert added["icons_in_copy"] == 1

        told, panel = start(c, allow_filled=True)                                 # Claude only pre-ticks the box
        assert panel["allow_filled"] is True and c.get(panel["base"] + "state").json()["allow_filled"] is True
        assert run_deck(c, panel["base"], deck).json()["settings"]["allow_filled"] is False   # the designer unticked
        assert run_deck(c, panel["base"], deck, allow_filled="on").json()["settings"]["allow_filled"] is True
        assert call(c, "get_icon_run", run_id=told["run_id"])[1]["filled_icons_allowed"] is True


def test_the_library_tab_reads_the_library_but_cant_change_it(env):
    with client() as c:
        _, panel = start(c)
        lib = c.get(panel["base"] + "library", headers={"Origin": PANEL}).json()
        assert {i["id"] for i in lib["icons"]} >= {"finance-growth-chart", "risk-shield"}
        icon = c.get(panel["base"] + "icons/risk-shield.svg?color=FD5108")
        assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
        assert c.get(panel["base"] + "icons/made-up.svg").status_code == 404
        assert c.post(panel["base"] + "library/icons", data={"id": "x"}).status_code in (404, 405)
        assert c.post(panel["base"] + "library/preview").status_code in (404, 405)
        other = c.get(panel["base"] + "library", headers={"Origin": "https://attacker.example"})
        assert other.status_code == 200 and "access-control-allow-origin" not in other.headers


def test_bad_decks_are_refused_and_the_run_stays_open(env):
    with client() as c:
        told, panel = start(c)
        (env / "notes.txt").write_text("not a deck")
        assert run_deck(c, panel["base"], env / "notes.txt", "notes.txt").json()["detail"] == \
            "Choose a PowerPoint deck (.pptx)."
        assert run_deck(c, panel["base"], env / "notes.txt", "fake.pptx").status_code == 400
        make_deck(env / "deck.pptx", ["Revenue growth"])
        assert run_deck(c, panel["base"], env / "deck.pptx", mode="standardize").status_code == 400
        assert run_deck(c, panel["base"], env / "deck.pptx", slides="2 to 5").status_code == 400
        assert call(c, "get_icon_run", run_id=told["run_id"])[1]["status"] == "waiting"
        assert call(c, "start_icon_run", slides="2 to 5")[0]
        assert call(c, "start_icon_run", mode="standardize")[0]


def test_a_broken_deck_fails_with_a_reason(env):
    import zipfile
    with zipfile.ZipFile(env / "broken.pptx", "w") as z:
        z.writestr("hello.txt", "a zip, but not a deck")
    with client() as c:
        told, panel = start(c)
        r = run_deck(c, panel["base"], env / "broken.pptx")
        assert r.status_code == 422 and "couldn't be read as a PowerPoint deck" in r.json()["detail"]
        failed = call(c, "get_icon_run", run_id=told["run_id"])[1]
        assert failed["status"] == "failed" and "couldn't be read as a PowerPoint deck" in failed["error"]


def test_expired_runs_and_wrong_links_lead_nowhere_and_are_deleted(env):
    with client() as c:
        told, panel = start(c)
        assert c.get(panel["link"][:-3] + "abc").status_code == 404              # a guessed token
        assert c.get(panel["link"][:-3] + "abc/state").status_code == 404
        assert c.get(panel["base"] + "jobs/000000000000/download").status_code == 404   # another run, same link
        run_dir = env / "jobs" / told["run_id"]
        connector._update(run_dir, expires=time.time() - 1)
        assert "expired" in c.get(panel["link"]).text
        assert "expired" in c.get(panel["base"] + "state").json()["detail"]
        assert call(c, "get_icon_run", run_id=told["run_id"])[0]
        call(c, "start_icon_run")                                                   # cleans up on the way
        assert not run_dir.exists()


def test_the_brand_font_reaches_the_page_and_claudes_panel(env):
    with client() as c:
        font = c.get("/fonts/Figtree-latin.woff2", headers={"Origin": PANEL})
        assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
        assert font.headers["access-control-allow-origin"] == "*"            # the panel loads it from another origin
        assert c.get("/fonts/missing.woff2").status_code == 404 and c.get("/fonts/icons.json").status_code == 404
    panel = connector.panel_html(BASE)
    assert f'url("{BASE}/fonts/Figtree-latin.woff2")' in panel and 'url("/fonts/' not in panel
    assert 'class="wordmark"' in panel and 'aria-label="Prezlab"' in panel


def test_the_run_page_and_the_sign_in_pages_carry_the_pl_mark_in_the_tab_but_the_panel_doesnt(env):
    with client() as c:
        mark = c.get("/favicon.png")
        assert mark.status_code == 200 and mark.headers["content-type"] == "image/png"
        assert mark.content.startswith(b"\x89PNG")
        assert c.get("/health").json()["slide_pictures"] in (True, False)   # LibreOffice reached, where it's hosted
    assert pages.TAB_ICON in connector._page("") and pages.TAB_ICON in pages.render("<p>hi</p>").body.decode()
    assert "favicon" not in connector.panel_html(BASE)                     # a panel has no tab


# --- who may call the connector ----------------------------------------------------------------------------------

SECRET = "s3cr3t-" + "x" * 33


def test_a_reachable_connector_is_never_open(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://prezlab-icons.onrender.com")
    for name in ("AUTH", "MCP_SECRET", "MCP_TOKEN"):
        monkeypatch.setattr(config, name, "")
    with pytest.raises(RuntimeError, match="must not be open"):
        auth.mode()
    for weak in ("short", "x" * 30 + "/../" + "y" * 10):                    # too short, or not safe in an address
        monkeypatch.setattr(config, "MCP_SECRET", weak)
        with pytest.raises(RuntimeError, match="at least 32"):
            auth.mode()
    monkeypatch.setattr(config, "MCP_SECRET", SECRET)
    assert auth.mode() == "secret"
    monkeypatch.setattr(config, "PUBLIC_URL", "http://prezlab-icons.onrender.com")
    with pytest.raises(RuntimeError, match="https"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_SECRET", "")
    monkeypatch.setattr(config, "MCP_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_TOKEN", "x" * 40)
    assert auth.mode() == "token"
    monkeypatch.setattr(config, "PUBLIC_URL", BASE)   # on Render without its address: refused, not left on localhost
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="ICON_PUBLIC_URL"):
        auth.mode()


def test_the_hosted_connector_needs_no_sign_in_only_its_secret_address(env, monkeypatch):
    monkeypatch.setattr(config, "MCP_SECRET", SECRET)
    with client("secret") as c:
        listed = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert c.post("/mcp", headers=MCP, json=listed).status_code == 404
        assert c.post("/mcp/" + "y" * 40, headers=MCP, json=listed).status_code == 404
        assert c.get("/.well-known/oauth-protected-resource").status_code == 404   # Claude asks for no sign-in
        tools = c.post(f"/mcp/{SECRET}", headers=MCP, json=listed).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"start_icon_run", "get_icon_run", "choose_icons", "search_icons"}
        r = c.post(f"/mcp/{SECRET}", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "start_icon_run", "arguments": {}}}).json()["result"]["structuredContent"]
        assert c.get(r["link"]).status_code == 200 and c.get(r["base"] + "state").status_code == 200


def test_token_mode_needs_the_token_for_tools_but_not_for_the_run_pages(monkeypatch):
    monkeypatch.setattr(config, "MCP_TOKEN", "s" * 40)
    with client("token") as c:
        assert c.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        wrong = {"Authorization": "Bearer " + "t" * 40}
        assert c.post("/mcp", headers={**MCP, **wrong}, json={"jsonrpc": "2.0", "id": 1,
                                                                "method": "tools/list"}).status_code == 401
        error, run = call(c, "start_icon_run", {"Authorization": "Bearer " + "s" * 40})
        assert not error and c.get(run["link"]).status_code == 200 and c.get(run["link"] + "/state").status_code == 200


# --- the web page for people without Claude: Odoo sign-in --------------------------------------------------------

ODOO = "https://prezlab.odoo.test"
TOTP_PAGE = '<form><input type="hidden" name="csrf_token" value="csrf-0123456789"><input name="totp_token"></form>'
LOGIN_PAGE = ('<form><input type="hidden" name="csrf_token" value="csrf-9876543210"><input name="login">'
              '<input type="password" name="password"></form>')
WEB = "/web"


class FakeOdoo:
    """Odoo's login endpoints, as Image Studio found them: designer@ signs in with a password, twofa@ also needs the
    code 123456 (or a trusted device), client@ is a portal user. Odoo sessions are s-<uid> once signed in, p-<uid>
    while waiting for a code. Like Odoo 17, finishing a sign-in retires the waiting session: from then on it's
    unknown, so the code page sends it to the login page and a code posted with it fails the CSRF check."""
    USERS = {"designer@prezlab.com": (7, False), "twofa@prezlab.com": (8, True), "client@example.com": (9, False)}

    def __init__(self):
        self.devices, self.passwords_sent, self.retired, self.codes_accepted = set(), 0, set(), 0

    def __call__(self, request):
        from urllib.parse import parse_qs
        cookies = dict(c.split("=", 1) for c in request.headers.get("cookie", "").split("; ") if "=" in c)
        session, path = cookies.get("session_id", ""), request.url.path
        if path == "/web/session/authenticate":
            self.passwords_sent += 1
            p = json.loads(request.content)["params"]
            uid, twofa = self.USERS.get(p["login"].lower(), (0, False))
            if not uid or p["password"] != "pw" or p["db"] != "prezlab":
                return httpx2.Response(200, json={"error": {"data": {"message": "Access Denied"}}})
            self.retired.discard(f"p-{uid}")
            return httpx2.Response(200, json={"result": {"uid": False if twofa else uid}},
                                   headers={"set-cookie": f"session_id={'p' if twofa else 's'}-{uid}; Path=/"})
        if path == "/web/login":
            return httpx2.Response(200, text=LOGIN_PAGE)
        waiting = session.startswith("p-") and session not in self.retired
        if path == "/web/login/totp" and request.method == "GET":
            if not waiting:
                return httpx2.Response(303, headers={"location": "/web/login"})
            if cookies.get("td_id") in self.devices:
                self.retired.add(session)
                return httpx2.Response(303, headers={"location": "/odoo", "set-cookie": f"session_id=s-{session[2:]}"})
            return httpx2.Response(200, text=TOTP_PAGE)
        if path == "/web/login/totp":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if not waiting:
                return httpx2.Response(400, text="Session expired (invalid CSRF token)")
            if form.get("totp_token") != "123456" or form.get("csrf_token") != "csrf-0123456789":
                return httpx2.Response(200, text=TOTP_PAGE)
            self.retired.add(session)
            self.codes_accepted += 1
            self.devices.add(f"td-{session}")
            return httpx2.Response(303, headers=[("location", "/odoo"), ("set-cookie", f"td_id=td-{session}; Path=/"),
                                                 ("set-cookie", f"session_id=s-{session[2:]}; Path=/")])
        uid = int(session[2:]) if session.startswith("s-") else 0
        if path == "/web/session/get_session_info":
            return httpx2.Response(200, json={"result": {"uid": uid or False}})
        if path == "/web/dataset/call_kw":
            login = next(k for k, v in self.USERS.items() if v[0] == uid)
            return httpx2.Response(200, json={"result": [{"login": login, "name": login.split("@")[0], "email": login,
                                                          "active": True, "share": uid == 9}]})
        return httpx2.Response(404)


@pytest.fixture
def fake_odoo(monkeypatch):
    fake = FakeOdoo()
    monkeypatch.setattr(config, "ODOO_URL", ODOO)
    monkeypatch.setattr(config, "ODOO_DB", "prezlab")
    monkeypatch.setattr(odoo, "TRANSPORT", httpx2.MockTransport(fake))
    monkeypatch.setattr(web, "_limits", web._Limits())
    monkeypatch.setattr(web, "_code_steps", web._CodeSteps())
    return fake


def runs(env):
    return sorted((env / "jobs").glob("*/run.json"))


def sign_in(c, login="designer@prezlab.com", password="pw"):
    return c.post(WEB, data={"login": login, "password": password}, follow_redirects=False)


def test_people_without_claude_sign_in_with_odoo_and_start_runs_on_the_web_page(env, fake_odoo):
    with client() as c:
        assert c.get("/", follow_redirects=False).headers["location"] == WEB
        first = c.get(WEB)
        assert first.status_code == 200 and 'name="password"' in first.text and first.headers["x-robots-tag"] == "noindex"
        assert c.post(WEB, data={"start": "1"}).status_code == 403 and not runs(env)   # not signed in

        wrong = sign_in(c, password="nope")
        assert wrong.status_code == 401 and "combination wasn&#x27;t recognized" in wrong.text and not c.cookies
        entered = sign_in(c)                                                     # signed in: straight to a run
        assert entered.status_code == 303 and entered.headers["location"].startswith(f"{BASE}/runs/")
        cookie = entered.headers["set-cookie"].lower()
        assert f"{web.SESSION}=" in cookie and "path=/web" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert "designer" not in cookie                                          # sealed: the browser can't read it
        page = c.get(entered.headers["location"])
        assert page.status_code == 200 and 'source: "web"' in page.text
        state = c.get(entered.headers["location"] + "/state").json()
        assert state["status"] == "waiting" and state["mode"] == "replace" and not state["allow_filled"]

        again = c.get(WEB)   # signed in: Start, and opening the page (a link preview too) starts nothing
        assert "Start with a deck" in again.text and "designer@prezlab.com" in again.text and len(runs(env)) == 1
        second = c.post(WEB, data={"start": "1"}, follow_redirects=False)
        assert second.status_code == 303 and second.headers["location"] != entered.headers["location"]
        assert len(runs(env)) == 2
        deck = env / "deck.pptx"   # a web run is a run like any other
        make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
        assert run_deck(c, second.headers["location"] + "/", deck, mode="add").status_code == 200

        out = c.post(WEB, data={"signout": "1"})
        assert "signed out" in out.text and 'name="password"' in c.get(WEB).text
        assert c.post(WEB, data={"start": "1"}).status_code == 403 and len(runs(env)) == 2


def test_two_factor_asks_for_a_code_once_per_browser(env, fake_odoo):
    with client() as c:
        asked = sign_in(c, "twofa@prezlab.com")
        assert "Enter your code" in asked.text and "Enter your code" in c.get(WEB).text
        sent = fake_odoo.passwords_sent
        wrong = c.post(WEB, data={"code": "000000"})
        assert wrong.status_code == 401 and "Enter your code" in wrong.text
        done = c.post(WEB, data={"code": "123 456"}, follow_redirects=False)
        assert done.status_code == 303 and fake_odoo.passwords_sent == sent      # the password went to Odoo once
        device = c.cookies.get(web.DEVICE)
        assert device and "td-" not in device                                     # the 2FA bypass key is sealed

        c.post(WEB, data={"signout": "1"})
        assert sign_in(c, "TwoFA@prezlab.com").status_code == 303                 # this browser: no code needed

        c.post(WEB, data={"signout": "1"})
        fake_odoo.devices.clear()                                                 # Odoo forgot the device
        assert "Enter your code" in sign_in(c, "twofa@prezlab.com").text
        assert 'name="password"' in c.post(WEB, data={"restart": "1"}).text and 'name="password"' in c.get(WEB).text

    with client() as other:                                                       # another browser is asked
        assert "Enter your code" in sign_in(other, "twofa@prezlab.com").text


def test_a_code_sent_again_after_it_worked_still_signs_in(env, fake_odoo):
    """A refresh, or a second click the browser sends after the first, repeats the code for an Odoo session that Odoo
    retired when it accepted the code. The repeat's answer is the one the browser shows, so it must be the sign-in."""
    with client() as c:
        sign_in(c, "twofa@prezlab.com")
        pending = c.cookies.get(web.PENDING)
        assert c.post(WEB, data={"code": "123456"}, follow_redirects=False).status_code == 303
    with client() as repeat:                     # the browser, still holding the code step: it never saw that answer
        repeat.cookies.set(web.PENDING, pending)
        again = repeat.post(WEB, data={"code": "123456"}, follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"].startswith(f"{BASE}/runs/")
        assert repeat.cookies.get(web.SESSION) and repeat.cookies.get(web.DEVICE)
    assert fake_odoo.codes_accepted == 1


def test_a_double_click_on_continue_sends_the_code_to_odoo_once(env, fake_odoo, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def slow(request):   # Odoo takes a moment over the code: long enough for a second click
        if request.method == "POST" and request.url.path == "/web/login/totp":
            await asyncio.sleep(0.3)
        return fake_odoo(request)

    monkeypatch.setattr(odoo, "TRANSPORT", httpx2.MockTransport(slow))
    with client() as c, ThreadPoolExecutor(2) as clicks:
        sign_in(c, "twofa@prezlab.com")
        sent = [clicks.submit(c.post, WEB, data={"code": "123456"}, follow_redirects=False) for _ in range(2)]
        answers = [click.result() for click in sent]
    assert [a.status_code for a in answers] == [303, 303] and fake_odoo.codes_accepted == 1
    assert all(a.headers["location"].startswith(f"{BASE}/runs/") for a in answers)


def test_a_code_for_a_sign_in_odoo_already_ended_asks_to_sign_in_again(env, fake_odoo):
    with client() as c:
        sign_in(c, "twofa@prezlab.com")
        fake_odoo.retired.add("p-8")             # Odoo ended the half-finished sign-in
        ended = c.post(WEB, data={"code": "123456"})
        assert ended.status_code == 401 and "expired" in ended.text and "changed its login page" not in ended.text
        assert 'name="password"' in ended.text and 'name="password"' in c.get(WEB).text


@pytest.mark.parametrize("login, message", [("client@example.com", "portal account"),
                                            ("nobody@prezlab.com", "recognized")])
def test_only_prezlab_staff_can_sign_in(env, fake_odoo, login, message):
    with client() as c:
        refused = sign_in(c, login)
        assert refused.status_code == 401 and message in refused.text and not runs(env)


def test_sign_in_attempts_are_rate_limited_and_forged_cookies_lead_nowhere(env, fake_odoo):
    with client() as c:
        codes = [sign_in(c, password="nope").status_code for _ in range(7)]
        assert codes == [401] * 6 + [429]
        forged = {"Cookie": f"{web.SESSION}=" + "A" * 80}
        assert 'name="password"' in c.get(WEB, headers=forged).text
        assert c.post(WEB, data={"start": "1"}, headers=forged).status_code == 403 and not runs(env)


def test_the_web_page_is_off_unless_odoo_is_set_up(env, monkeypatch):
    with client() as c:
        assert c.get(WEB).status_code == 404 and c.get("/").status_code == 404
    monkeypatch.setattr(config, "ODOO_URL", ODOO)
    with pytest.raises(RuntimeError, match="both ODOO_URL and ODOO_DB"):
        connector.build_app("none")
    monkeypatch.setattr(config, "ODOO_DB", "prezlab")
    monkeypatch.setattr(config, "ODOO_URL", "http://prezlab.odoo.test")
    with pytest.raises(RuntimeError, match="ODOO_URL must be an https"):
        connector.build_app("none")
    monkeypatch.setattr(config, "ODOO_URL", ODOO)
    monkeypatch.setattr(config, "PUBLIC_URL", "http://prezlab-icons.onrender.com")
    with pytest.raises(RuntimeError, match="ICON_PUBLIC_URL must be an https"):
        web.check()


# --- the test UI's runs share the same apply step ---------------------------------------------------------------

def test_the_test_ui_still_adds_chosen_suggestions(env, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "library", Library(env / "lib"))
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with TestClient(main.app) as c, open(deck, "rb") as f:
        job = c.post("/jobs", files={"deck": ("deck.pptx", f, PPTX)}, data={"mode": "suggest"}).json()
        assert len(job["picks"]) == 3
        assert c.post(f"/jobs/{job['job']}/apply", json={"picks": [], "choices": {}}).status_code == 400
        added = c.post(f"/jobs/{job['job']}/apply", json={"picks": [1]}).json()
        assert added["added"] == 1 and added["download"] == f"jobs/{job['job']}/download"
        (env / "copy.pptx").write_bytes(c.get(f"/jobs/{job['job']}/download").content)
        assert icons_in(env / "copy.pptx") == ["Icon - people-team"]
