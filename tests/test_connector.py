"""The Claude connector (app/connector.py, app/auth.py) and the test UI's runs (app/main.py, app/jobs.py), driven over
HTTP the way Claude and a designer's browser use them. Offline: no Claude API calls, no Entra ID."""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
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
    monkeypatch.setattr(config, "PUBLIC_URL", "https://icons.example.com")
    monkeypatch.setattr(config, "AUTH", "")
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", "")
    monkeypatch.setattr(config, "MCP_TOKEN", "")
    with pytest.raises(RuntimeError, match="needs sign-in"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        auth.mode()
    monkeypatch.setattr(config, "MCP_TOKEN", "x" * 40)
    assert auth.mode() == "token"


def test_token_mode_needs_the_token_for_tools_but_not_for_the_run_pages(monkeypatch):
    monkeypatch.setattr(config, "MCP_TOKEN", "s" * 40)
    with client("token") as c:
        assert c.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        wrong = {"Authorization": "Bearer " + "t" * 40}
        assert c.post("/mcp", headers={**MCP, **wrong}, json={"jsonrpc": "2.0", "id": 1,
                                                                "method": "tools/list"}).status_code == 401
        error, run = call(c, "start_icon_run", {"Authorization": "Bearer " + "s" * 40})
        assert not error and c.get(run["upload_link"]).status_code == 200


TENANT, CLIENT_ID = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"
APP_ID_URI = f"{BASE}/mcp"


class FakeEntra:
    """Entra's signing keys, for tokens made in the test."""

    def __init__(self):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict({**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key())),
                                    "kid": "k1"})

    def token(self, key=None, **changes):
        now = int(time.time())
        claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT_ID, "tid": TENANT,
                  "oid": "designer-oid", "azp": "claude-client", "scp": "access_as_user", "iat": now,
                  "exp": now + 3600, **changes}
        return jwt.encode(claims, key or self.private, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def entra(monkeypatch):
    fake = FakeEntra()
    monkeypatch.setattr(config, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth.jwt, "PyJWKClient", lambda *a, **k: fake)
    return fake


@pytest.mark.parametrize("changes, accepted", [
    ({}, True),
    ({"aud": APP_ID_URI, "iss": f"https://sts.windows.net/{TENANT}/"}, True),   # a v1 token, for the App ID URI
    ({"aud": "someone-else"}, False),
    ({"tid": "another-tenant"}, False),
    ({"iss": "https://login.microsoftonline.com/another-tenant/v2.0"}, False),
    ({"exp": int(time.time()) - 600}, False),
])
def test_entra_tokens_are_accepted_only_from_the_tenant_for_this_app(entra, changes, accepted):
    import anyio
    verifier = auth.EntraTokenVerifier(TENANT, CLIENT_ID, APP_ID_URI, entra)
    token = anyio.run(verifier.verify_token, entra.token(**changes))
    assert (token is not None) == accepted
    if accepted:
        assert token.subject == "designer-oid" and token.scopes == [f"{APP_ID_URI}/access_as_user"]


def test_a_token_signed_by_another_key_is_refused(entra):
    import anyio
    verifier = auth.EntraTokenVerifier(TENANT, CLIENT_ID, APP_ID_URI, entra)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert anyio.run(verifier.verify_token, entra.token(key=other)) is None


def test_entra_mode_points_claude_to_sign_in_and_runs_belong_to_the_designer(env, entra):
    with client("entra") as c:
        r = c.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert 'resource_metadata="http://127.0.0.1:8001/.well-known/oauth-protected-resource/mcp"' in \
            r.headers["www-authenticate"]
        meta = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == APP_ID_URI
        assert meta["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]
        assert meta["scopes_supported"] == [f"{APP_ID_URI}/access_as_user"]

        no_scope = {"Authorization": f"Bearer {entra.token(scp='User.Read')}"}
        assert c.post("/mcp", headers={**MCP, **no_scope}, json={"jsonrpc": "2.0", "id": 1,
                                                                   "method": "tools/list"}).status_code == 403
        error, run = call(c, "start_icon_run", {"Authorization": f"Bearer {entra.token()}"})
        assert not error
        assert json.loads((env / "jobs" / run["run_id"] / "run.json").read_text())["owner"] == "designer-oid"
        other = {"Authorization": f"Bearer {entra.token(oid='someone-else')}"}
        assert call(c, "get_icon_run", other, run_id=run["run_id"])[0]


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
