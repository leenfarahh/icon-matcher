"""The Claude connector (app/connector.py, app/auth.py) and the test UI's runs (app/main.py, app/jobs.py), driven over
HTTP the way Claude and a designer's browser use them. Offline: no Claude API calls."""
import json
import shlex
import time

import pytest
from pptx import Presentation
from pptx.util import Inches
from starlette.testclient import TestClient

from app import auth, config, connector
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
        assert state.json() == {"status": "waiting", "error": "", "mode": "suggest", "slides": "2,3", "attached": None,
                                "result": None}
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


def test_a_deck_attached_in_the_chat_is_ready_in_the_panel(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        told, panel = start(c, mode="add", attached_deck="/mnt/user-data/uploads/Client deck.pptx")
        sent = told["attached_deck"]
        assert sent["upload_url"] == panel["base"] + "deck" and "attached_deck.command" in told["next"]
        assert shlex.split(sent["command"]) == ["curl", "-sS", "--fail-with-body", "-F",
                                                'deck=@"/mnt/user-data/uploads/Client deck.pptx"', sent["upload_url"]]
        state = lambda: c.get(panel["base"] + "state", headers={"Origin": PANEL}).json()
        assert state()["attached"] == {"name": "Client deck.pptx", "ready": False}      # the panel waits for it
        no_deck = c.post(panel["base"] + "jobs", files=NO_FILE, data={"mode": "add"}, headers={"Origin": PANEL})
        assert no_deck.json()["detail"] == "Choose a PowerPoint deck (.pptx)."

        with open(deck, "rb") as f:                                        # Claude's curl, from its code tools
            arrived = c.post(sent["upload_url"], files={"deck": ("Client deck.pptx", f, PPTX)})
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
            assert c.post(sent["upload_url"], files={"deck": ("notes.pptx", f, PPTX)}).status_code == 400
        with open(deck, "rb") as f:
            assert c.post(panel["link"][:-3] + "abc/deck", files={"deck": ("d.pptx", f, PPTX)}).status_code == 404
        assert call(c, "start_icon_run", attached_deck="/mnt/user-data/uploads/old.ppt")[0]


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
        assert c.get(job + "slides/1.png").status_code == 404                     # no PowerPoint here: a blank slide

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
