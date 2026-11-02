"""The Claude connector (app/connector.py, app/auth.py) and the test UI's runs (app/main.py, app/jobs.py), driven over
HTTP the way Claude and a designer's browser use them. Offline: no Claude API calls, and a stand-in for Odoo."""
import json
import time

import httpx2
import pytest
from pptx import Presentation
from pptx.util import Inches
from starlette.testclient import TestClient

from app import auth, config, connector, odoo
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
    monkeypatch.setattr(config, "AUTH_DB", tmp_path / "jobs" / "auth.sqlite3")
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


def upload(c, link, path, name="Client deck.pptx"):
    with open(path, "rb") as f:
        return c.post(link, files={"deck": (name, f, PPTX)}, follow_redirects=False)


def finished(c, run_id, headers=None):
    end = time.time() + 30
    while time.time() < end:
        error, run = call(c, "get_icon_run", headers, run_id=run_id)
        assert not error, run
        if run["status"] not in ("waiting", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("the run didn't finish")


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


# --- the run, as Claude and the designer see it -----------------------------------------------------------------

def test_a_deck_uploaded_through_the_link_comes_back_as_a_copy_with_icons(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        error, run = call(c, "start_icon_run", mode="add")
        assert not error and run["status"] == "waiting"
        assert run["upload_link"].startswith(f"{BASE}/runs/{run['run_id']}/")
        assert "Upload your deck" in c.get(run["upload_link"]).text
        assert call(c, "get_icon_run", run_id=run["run_id"])[1]["status"] == "waiting"

        r = upload(c, run["upload_link"], deck)
        assert r.status_code == 303 and r.headers["location"] == run["upload_link"]
        done = finished(c, run["run_id"])
        assert done["status"] == "done" and done["flagged"] == []
        assert [p["icon"] for p in done["placed"]] == ["finance-growth-chart", "people-team", "risk-shield"]
        assert done["copy"]["icons"] == 3 and done["copy"]["download"] == run["upload_link"] + "/download"

        page = c.get(run["upload_link"])
        assert "3 icons in the new copy" in page.text and page.headers["referrer-policy"] == "no-referrer"
        copy = c.get(done["copy"]["download"])
        assert copy.status_code == 200 and "Client%20deck_icons.pptx" in copy.headers["content-disposition"]
        (env / "copy.pptx").write_bytes(copy.content)
        assert len(icons_in(env / "copy.pptx")) == 3
        assert icons_in(env / "jobs" / run["run_id"] / "Client deck.pptx") == []    # the upload is never changed
        assert upload(c, run["upload_link"], deck).status_code == 303                # one upload per run
        assert finished(c, run["run_id"])["copy"]["icons"] == 3


def test_suggestions_go_into_the_copy_only_once_chosen(env):
    deck = env / "deck.pptx"
    make_deck(deck, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    with client() as c:
        run = call(c, "start_icon_run", mode="suggest")[1]
        upload(c, run["upload_link"], deck)
        done = finished(c, run["run_id"])
        assert len(done["suggested"]) == 3 and done["copy"] is None
        assert "Suggestions ready" in c.get(run["upload_link"]).text
        assert c.get(run["upload_link"] + "/download").status_code == 404

        error, chosen = call(c, "choose_icons", run_id=run["run_id"], picks=[0, 2])
        assert not error and chosen["icons_in_copy"] == 2
        (env / "copy.pptx").write_bytes(c.get(chosen["download"]).content)
        assert sorted(icons_in(env / "copy.pptx")) == ["Icon - finance-growth-chart", "Icon - risk-shield"]
        assert call(c, "choose_icons", run_id=run["run_id"], picks=[7])[1] == "unknown suggestion: run the deck again"


def test_flagged_placeholders_get_the_icons_the_designer_chose(env):
    deck = env / "deck.pptx"
    flagged_deck(deck)
    with client() as c:
        run = call(c, "start_icon_run")[1]
        upload(c, run["upload_link"], deck)
        done = finished(c, run["run_id"])
        assert done["placed"] == [] and done["copy"]["icons"] == 0
        assert [f["label"] for f in done["flagged"]] == ["Revenue growth", "Quarterly outlook"]
        assert all(f["can_choose"] for f in done["flagged"])
        assert "2 placeholders left for you to choose" in c.get(run["upload_link"]).text

        assert call(c, "choose_icons", run_id=run["run_id"], choices={"0": "made-up"})[1] == \
            "choose approved outline icons from the library, or upload one"
        error, chosen = call(c, "choose_icons", run_id=run["run_id"],
                             choices={"0": "finance-growth-chart", "1": "risk-shield"})
        assert not error and chosen["icons_in_copy"] == 2
        (env / "copy.pptx").write_bytes(c.get(chosen["download"]).content)
        shapes = [s.name for s in Presentation(env / "copy.pptx").slides[0].shapes]
        assert sorted(n for n in shapes if n.startswith("Icon - ")) == ["Icon - finance-growth-chart",
                                                                         "Icon - risk-shield"]
        assert "PL logo" not in shapes                                                # the placeholders are gone


def test_search_finds_icons_by_meaning_house_pick_first(env):
    with client() as c:
        found = call(c, "search_icons", query="Growth", limit=5)[1]
        assert found["icons"][0]["icon"] == "finance-growth-chart"
        assert call(c, "search_icons", query="zebra crossing")[1]["icons"] == []


def test_bad_uploads_are_refused_and_the_run_stays_open(env):
    with client() as c:
        run = call(c, "start_icon_run")[1]
        (env / "notes.txt").write_text("not a deck")
        assert upload(c, run["upload_link"], env / "notes.txt", "notes.txt").status_code == 400
        assert upload(c, run["upload_link"], env / "notes.txt", "fake.pptx").status_code == 400
        assert call(c, "get_icon_run", run_id=run["run_id"])[1]["status"] == "waiting"
        assert call(c, "start_icon_run", slides="2 to 5")[0]
        assert call(c, "start_icon_run", mode="standardize")[0]


def test_a_broken_deck_fails_with_a_reason(env):
    import zipfile
    with zipfile.ZipFile(env / "broken.pptx", "w") as z:
        z.writestr("hello.txt", "a zip, but not a deck")
    with client() as c:
        run = call(c, "start_icon_run")[1]
        upload(c, run["upload_link"], env / "broken.pptx")
        failed = finished(c, run["run_id"])
        assert failed["status"] == "failed" and "couldn't be read as a PowerPoint deck" in failed["error"]
        assert "This run didn't finish" in c.get(run["upload_link"]).text


def test_expired_runs_and_wrong_links_lead_nowhere_and_are_deleted(env):
    with client() as c:
        run = call(c, "start_icon_run")[1]
        assert c.get(run["upload_link"][:-3] + "abc").status_code == 404              # a guessed token
        run_dir = env / "jobs" / run["run_id"]
        connector._update(run_dir, expires=time.time() - 1)
        assert "expired" in c.get(run["upload_link"]).text
        assert call(c, "get_icon_run", run_id=run["run_id"])[0]
        call(c, "start_icon_run")                                                       # cleans up on the way
        assert not run_dir.exists()


def test_a_run_belongs_to_whoever_started_it(env, monkeypatch):
    with client() as c:
        monkeypatch.setattr(connector, "_caller", lambda: "designer-a")
        run = call(c, "start_icon_run")[1]
        assert call(c, "get_icon_run", run_id=run["run_id"])[1]["status"] == "waiting"
        monkeypatch.setattr(connector, "_caller", lambda: "designer-b")
        error, message = call(c, "get_icon_run", run_id=run["run_id"])
        assert error and message.startswith(f"no run '{run['run_id']}'")


# --- sign-in ----------------------------------------------------------------------------------------------------

def test_a_reachable_connector_never_runs_without_sign_in(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://prezlab-icons.onrender.com")
    for name in ("AUTH", "ODOO_URL", "ODOO_DB", "MCP_TOKEN", "ODOO_TD_ENCRYPTION_KEY"):
        monkeypatch.setattr(config, name, "")
    with pytest.raises(RuntimeError, match="needs sign-in"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_TOKEN", "x" * 40)
    assert auth.mode() == "token"
    monkeypatch.setattr(config, "ODOO_URL", ODOO)
    with pytest.raises(RuntimeError, match="ODOO_DB"):
        auth.mode()
    monkeypatch.setattr(config, "ODOO_DB", "prezlab")
    assert auth.mode() == "odoo"
    monkeypatch.setattr(config, "ODOO_TD_ENCRYPTION_KEY", "too short")
    with pytest.raises(RuntimeError, match="32"):
        auth.mode()
    monkeypatch.setattr(config, "ODOO_TD_ENCRYPTION_KEY", "")
    monkeypatch.setattr(config, "PUBLIC_URL", "http://prezlab-icons.onrender.com")
    with pytest.raises(RuntimeError, match="https"):
        auth.mode()
    monkeypatch.setattr(config, "PUBLIC_URL", BASE)   # on Render without its address: refused, not left on localhost
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="ICON_PUBLIC_URL"):
        auth.mode()


def test_token_mode_needs_the_token_for_tools_but_not_for_the_run_pages(monkeypatch):
    monkeypatch.setattr(config, "MCP_TOKEN", "s" * 40)
    with client("token") as c:
        assert c.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        wrong = {"Authorization": "Bearer " + "t" * 40}
        assert c.post("/mcp", headers={**MCP, **wrong}, json={"jsonrpc": "2.0", "id": 1,
                                                                "method": "tools/list"}).status_code == 401
        error, run = call(c, "start_icon_run", {"Authorization": "Bearer " + "s" * 40})
        assert not error and c.get(run["upload_link"]).status_code == 200


ODOO = "https://prezlab.odoo.test"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
TOTP_PAGE = '<form><input type="hidden" name="csrf_token" value="csrf-0123456789"><input name="totp_token"></form>'


class FakeOdoo:
    """Odoo's login endpoints, as Image Studio found them: designer@ signs in with a password, twofa@ also needs the
    code 123456 (or a trusted device), client@ is a portal user. Odoo sessions are s-<uid> once signed in, p-<uid>
    while waiting for a code."""
    USERS = {"designer@prezlab.com": (7, False), "twofa@prezlab.com": (8, True), "client@example.com": (9, False)}

    def __init__(self):
        self.devices, self.passwords_sent = set(), 0

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
            return httpx2.Response(200, json={"result": {"uid": False if twofa else uid}},
                                   headers={"set-cookie": f"session_id={'p' if twofa else 's'}-{uid}; Path=/"})
        if path == "/web/login/totp" and request.method == "GET":
            if session.startswith("p-") and cookies.get("td_id") in self.devices:
                return httpx2.Response(303, headers={"location": "/odoo", "set-cookie": f"session_id=s-{session[2:]}"})
            return httpx2.Response(200, text=TOTP_PAGE if session.startswith("p-") else "<form>login</form>")
        if path == "/web/login/totp":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("totp_token") != "123456" or form.get("csrf_token") != "csrf-0123456789":
                return httpx2.Response(200, text=TOTP_PAGE)
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
def odoo_signin(monkeypatch):
    fake = FakeOdoo()
    monkeypatch.setattr(config, "ODOO_URL", ODOO)
    monkeypatch.setattr(config, "ODOO_DB", "prezlab")
    monkeypatch.setattr(config, "ODOO_TD_ENCRYPTION_KEY", "k" * 40)
    monkeypatch.setattr(odoo, "TRANSPORT", httpx2.MockTransport(fake))
    return fake


def pkce():
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def register(c, redirect=CALLBACK):
    return c.post("/register", json={"redirect_uris": [redirect], "client_name": "Claude",
                                     "token_endpoint_auth_method": "none",
                                     "grant_types": ["authorization_code", "refresh_token"],
                                     "response_types": ["code"]})


def start_signin(c, client_id, challenge):
    """Claude's /authorize request: the sign-in page it sends the designer to."""
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                                    "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
                                    "scope": "icons", "resource": f"{BASE}/mcp"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{BASE}/signin/"), r.text
    return r.headers["location"]


def back_to_claude(r):
    """The code and state the sign-in page sends the designer back to Claude with."""
    from urllib.parse import parse_qs, urlsplit
    assert r.status_code == 303 and r.headers["location"].startswith(CALLBACK + "?"), r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    return query["code"][0], query["state"][0]


def signed_in(c, login="designer@prezlab.com"):
    """Sign in the way Claude and a designer do. Returns (client_id, tokens)."""
    client_id = register(c).json()["client_id"]
    verifier, challenge = pkce()
    page = start_signin(c, client_id, challenge)
    code, _ = back_to_claude(c.post(page, data={"login": login, "password": "pw"}, follow_redirects=False))
    tokens = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                    "client_id": client_id, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def tools_list(c, headers=None):
    return c.post("/mcp", headers={**MCP, **(headers or {})}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def test_designers_sign_in_with_odoo_and_runs_belong_to_them(env, odoo_signin):
    with client("odoo") as c:
        r = tools_list(c)
        assert r.status_code == 401
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
        meta = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == f"{BASE}/mcp" and meta["authorization_servers"] == [BASE]
        server = c.get("/.well-known/oauth-authorization-server").json()
        assert server["authorization_endpoint"] == f"{BASE}/authorize" and server["registration_endpoint"]
        assert register(c, "https://attacker.example/callback").status_code == 400   # Claude's return addresses only

        client_id = register(c).json()["client_id"]
        verifier, challenge = pkce()
        page = start_signin(c, client_id, challenge)
        form = c.get(page)
        assert "Sign in to Prezlab Icons" in form.text and "claude.ai" in form.text
        assert form.headers["x-frame-options"] == "DENY" and form.headers["cache-control"] == "no-store"
        wrong = c.post(page, data={"login": "designer@prezlab.com", "password": "nope"})
        assert wrong.status_code == 401 and "recognized" in wrong.text
        code, state = back_to_claude(c.post(page, data={"login": "designer@prezlab.com", "password": "pw"},
                                            follow_redirects=False))
        assert state == "st-1"
        assert c.get(page).status_code == 404                                  # the sign-in link is used up

        token_request = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                         "client_id": client_id, "code_verifier": "not-the-verifier-" + "x" * 30}
        assert c.post("/token", data=token_request).json()["error"] == "invalid_grant"   # PKCE
        tokens = c.post("/token", data={**token_request, "code_verifier": verifier}).json()
        assert tokens["scope"] == "icons" and tokens["expires_in"] == 3600 and tokens["refresh_token"]

        assert tools_list(c, bearer(tokens)).status_code == 200
        error, run = call(c, "start_icon_run", bearer(tokens))
        assert not error
        assert json.loads((env / "jobs" / run["run_id"] / "run.json").read_text())["owner"] == "designer@prezlab.com"
        _, other = signed_in(c)                                                # another sign-in, same designer
        assert not call(c, "get_icon_run", bearer(other), run_id=run["run_id"])[0]

        assert c.post("/token", data={**token_request, "code_verifier": verifier}).status_code == 400   # code replay
        assert tools_list(c, bearer(tokens)).status_code == 401              # ...ends the sign-in it made
        assert (env / "jobs" / "auth.sqlite3").exists()


def test_refresh_tokens_rotate_and_a_reused_one_ends_the_sign_in(env, odoo_signin):
    with client("odoo") as c:
        client_id, first = signed_in(c)
        refresh = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": client_id}
        second = c.post("/token", data=refresh).json()
        assert second["refresh_token"] != first["refresh_token"] and tools_list(c, bearer(second)).status_code == 200
        assert c.post("/token", data=refresh).json()["error"] == "invalid_grant"    # the old one, again
        assert tools_list(c, bearer(second)).status_code == 401                   # ...revokes the newer tokens too
        _, third = signed_in(c)
        assert tools_list(c, bearer(third)).status_code == 200                    # other sign-ins carry on


def test_two_factor_sign_in_asks_for_a_code_once_per_device(env, odoo_signin):
    with client("odoo") as c:
        client_id = register(c).json()["client_id"]
        page = start_signin(c, client_id, pkce()[1])
        asked = c.post(page, data={"login": "twofa@prezlab.com", "password": "pw"})
        assert "Enter your code" in asked.text and "Enter your code" in c.get(page).text
        sent = odoo_signin.passwords_sent
        wrong = c.post(page, data={"code": "000000"})
        assert wrong.status_code == 401 and "Enter your code" in wrong.text
        back_to_claude(c.post(page, data={"code": "123 456"}, follow_redirects=False))
        assert odoo_signin.passwords_sent == sent                                  # the password went to Odoo once
        sealed = connector.oauth.Store(env / "jobs" / "auth.sqlite3").row("SELECT sealed FROM devices")["sealed"]
        assert "td-" not in sealed                                                 # the 2FA bypass key is sealed

        again = start_signin(c, client_id, pkce()[1])                              # next sign-in: no code needed
        back_to_claude(c.post(again, data={"login": "TwoFA@prezlab.com", "password": "pw"}, follow_redirects=False))

        restart = start_signin(c, client_id, pkce()[1])
        odoo_signin.devices.clear()                                                # Odoo forgot the device
        assert "Enter your code" in c.post(restart, data={"login": "twofa@prezlab.com", "password": "pw"}).text
        assert "Sign in to Prezlab Icons" in c.post(restart, data={"restart": "1"}).text


@pytest.mark.parametrize("login, message", [
    ("client@example.com", "portal account"),
    ("nobody@prezlab.com", "recognized"),
])
def test_only_prezlab_staff_can_sign_in(env, odoo_signin, login, message):
    with client("odoo") as c:
        page = start_signin(c, register(c).json()["client_id"], pkce()[1])
        refused = c.post(page, data={"login": login, "password": "pw"}, follow_redirects=False)
        assert refused.status_code == 401 and message in refused.text


def test_sign_in_attempts_are_rate_limited(env, odoo_signin):
    with client("odoo") as c:
        page = start_signin(c, register(c).json()["client_id"], pkce()[1])
        codes = [c.post(page, data={"login": "designer@prezlab.com", "password": "nope"}).status_code
                 for _ in range(7)]
        assert codes == [401] * 6 + [429]
        assert c.get(f"{BASE}/signin/made-up").status_code == 404


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
