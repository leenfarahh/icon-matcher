"""Usage (app/usage.py) and its page (app/usage_page.py), driven the way Claude, a designer's browser and an owner use
them. Offline: no Claude API calls."""
import csv
import io
import sqlite3
import time
from types import SimpleNamespace

import pytest

from app import concepts, config, usage, usage_page, web
from test_connector import (BASE, MCP, WEB, call, client, env, fake_odoo, flagged_deck, run_deck,  # noqa: F401 (fixtures)
                            sign_in, start)
from test_pipeline import make_deck

LABELS = ["Revenue growth of 22%", "Team expansion", "Risk management"]


def rows(event: str | None = None) -> list[dict]:
    return [e for e in usage.events(0) if event is None or e["event"] == event]


def handshake(c, name: str, agent: str, panel: bool = True):
    c.post("/mcp", headers={**MCP, "User-Agent": agent}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "clientInfo": {"name": name, "version": "1"},
            "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {}} if panel else {}}}})


# --- what is recorded -------------------------------------------------------------------------------------------

def test_a_claude_run_is_counted_from_start_to_download_with_no_deck_content(env):
    deck = env / "deck.pptx"
    make_deck(deck, LABELS)
    with client() as c:
        handshake(c, "claude-ai", "Claude-User")
        told, panel = start(c, {"User-Agent": "Claude-User"}, mode="add")
        assert run_deck(c, panel["base"], deck, "Client deck.pptx", mode="add").status_code == 200
        assert c.get(panel["base"] + f"jobs/{told['run_id']}/download").status_code == 200

    [started] = rows("run_started")
    assert started["run"] == told["run_id"] and started["source"] == "claude" and started["app"] == "claude-ai"
    assert started["person"] is None and started["mode"] == "add"
    [ran] = rows("deck_run")
    assert ran["ok"] and ran["placed"] == 3 and ran["icons"] == ["finance-growth-chart", "people-team", "risk-shield"]
    assert ran["matching"] == "keywords" and ran["app"] == "claude-ai" and isinstance(ran["seconds"], float)
    assert [e["event"] for e in rows()] == ["run_started", "deck_run", "download"]

    kept = usage.path().read_bytes()                       # counts and icon ids: nothing from the deck itself
    for text in ["Client deck", "Revenue", "Team expansion", "Risk management", panel["link"].rsplit("/", 1)[1]]:
        assert text.encode() not in kept

    s = usage.summary(30)
    assert (s["started"], s["runs"], s["deck_runs"], s["downloaded"], s["placed"]) == (1, 1, 1, 1, 3)
    assert s["by_app"] == {"claude-ai": {"started": 1, "ran": 1, "deck_runs": 1, "downloaded": 1, "placed": 3}}
    assert sum(p["claude"] for p in s["series"]) == 1 and sum(p["web"] for p in s["series"]) == 0


def test_placeholders_filled_by_hand_count_as_designers_choices(env):
    deck = env / "deck.pptx"
    flagged_deck(deck)
    with client() as c:
        told, panel = start(c)
        report = run_deck(c, panel["base"], deck).json()
        assert report["flags"] and not report["picks"]
        error, done = call(c, "choose_icons", run_id=told["run_id"], choices={"0": "people-team", "1": "risk-shield"})
        assert not error, done
    [ran] = rows("deck_run")
    assert ran["placeholders"] == 2 and ran["placeholders_filled"] == 0 and ran["flagged"] == 2
    [added] = rows("icons_added")
    assert added["via"] == "chat" and added["chosen"] == 2 and added["uploads"] == 0
    assert added["icons"] == ["people-team", "risk-shield"]
    s = usage.summary(30)
    assert (s["placeholders"], s["placeholders_filled"], s["chosen"]) == (2, 0, 2)
    assert dict(s["icons"]) == {"people-team": 1, "risk-shield": 1}


def test_the_claude_app_comes_from_the_handshake_or_the_request_itself(env):
    with client() as c:
        handshake(c, "claude-ai", "Claude-User")
        handshake(c, "claude-code", "claude-code/2.1.0 (cli)", panel=False)
        start(c, {"User-Agent": "Claude-User"})
        start(c, {"User-Agent": "claude-code/2.1.0 (cli)"})
        start(c, {"User-Agent": "Something/1.0"})                     # never seen connecting: its user agent
        handshake(c, "office-addin", "Claude-User", panel=False)      # two apps on one agent: the agent, not a guess
        start(c, {"User-Agent": "Claude-User"})
        c.post("/mcp", headers={**MCP, "User-Agent": "Claude-User"}, json={   # newer protocols: named in each request
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "start_icon_run", "arguments": {},
                "_meta": {"io.modelcontextprotocol/clientInfo": {"name": "claude-ai", "version": "2"}}}})
    assert [e["app"] for e in rows("run_started")] == ["claude-ai", "claude-code", "Something", "Claude-User",
                                                       "claude-ai"]
    seen = {(c["name"], c["agent"]): (c["panel"], c["handshakes"]) for c in usage.clients()}
    assert seen == {("claude-ai", "Claude-User"): (True, 1), ("claude-code", "claude-code/2.1.0 (cli)"): (False, 1),
                    ("office-addin", "Claude-User"): (False, 1)}


def test_searches_keep_their_words_only_when_nothing_matched(env):
    with client() as c:
        assert call(c, "search_icons", query="growth")[1]["icons"]
        assert not call(c, "search_icons", query="  quantum   " + "z" * 100)[1]["icons"]
    found, missed = rows("search")
    assert found["results"] > 0 and found["query"] == ""
    assert missed["results"] == 0 and missed["query"] == ("quantum " + "z" * 100)[:usage.QUERY_CHARS]
    assert usage.summary(30)["misses"] == [(missed["query"], 1)]


def test_a_failed_deck_run_is_counted_as_failed(env):
    (env / "broken.pptx").write_bytes(b"PK\x03\x04 not really a deck")
    with client() as c:
        _, panel = start(c)
        assert run_deck(c, panel["base"], env / "broken.pptx").status_code == 422
    [ran] = rows("deck_run")
    assert not ran["ok"] and ran["failure"] == "unreadable"
    assert usage.summary(30)["failed"] == 1 and usage.summary(30)["runs"] == 0


def test_claudes_tokens_add_up_across_the_threads_a_deck_run_uses():
    reply = SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=40,
                            output_tokens=7)
    with usage.counting_tokens() as tokens:
        concepts._parallel(lambda b: [usage.count_tokens(reply) for _ in b], [[1, 2], [3], [4, 5, 6]])
        usage.count_tokens(reply)
    assert tokens.requests == 7 and tokens.counts == {
        "input_tokens": 700, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 280, "output_tokens": 49}
    usage.count_tokens(reply)                                  # outside a deck run: counted nowhere, no error
    assert tokens.requests == 7


def test_recording_never_gets_in_a_designers_way(env, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(usage, "_db", broken)
    deck = env / "deck.pptx"
    make_deck(deck, LABELS)
    with client() as c:
        told, panel = start(c)
        assert run_deck(c, panel["base"], deck, mode="add").status_code == 200
        assert c.get(panel["base"] + f"jobs/{told['run_id']}/download").status_code == 200


def test_recording_can_be_turned_off(env, monkeypatch):
    monkeypatch.setattr(config, "USAGE", False)
    with client() as c:
        start(c)
    assert not usage.path().exists()


def test_the_summary_counts_runs_in_the_period_by_day_and_by_week(env):
    now = time.time()
    for days_ago, source, run in ((1, "claude", "a"), (1, "web", "b"), (20, "claude", "c"), (60, "web", "d")):
        usage.record("deck_run", {"id": run, "source": source, "app": "claude-ai"}, ok=True, placed=2)
        with sqlite3.connect(usage.path()) as db:
            db.execute("UPDATE events SET at = ? WHERE run = ?", (now - days_ago * 86400, run))
    week, month, quarter = usage.summary(7, now), usage.summary(30, now), usage.summary(90, now)
    assert (week["runs"], month["runs"], quarter["runs"]) == (2, 3, 4)
    assert not month["weekly"] and len(month["series"]) == 31 and quarter["weekly"]
    assert all(p["date"].weekday() == 0 for p in quarter["series"])
    assert sum(p["web"] for p in quarter["series"]) == 2 and month["by_app"]["Web page"]["ran"] == 1


# --- the page ---------------------------------------------------------------------------------------------------

@pytest.fixture
def owners(monkeypatch, fake_odoo):
    monkeypatch.setattr(config, "USAGE_ADMINS", {"designer@prezlab.com"})
    return fake_odoo


def test_owners_sign_in_and_land_on_the_usage_page_without_starting_a_run(env, owners):
    deck = env / "deck.pptx"
    make_deck(deck, LABELS)
    with client() as c:
        _, panel = start(c, mode="add")
        run_deck(c, panel["base"], deck, mode="add")
        away = c.get(web.USAGE_PATH, follow_redirects=False)              # not signed in: sign in first
        assert away.status_code == 303 and away.headers["location"] == f"{WEB}?to=usage"
        entered = c.post(f"{WEB}?to=usage", data={"login": "designer@prezlab.com", "password": "pw"},
                         follow_redirects=False)
        assert entered.headers["location"] == web.USAGE_PATH
        assert len(rows("run_started")) == 1                             # signing in here started no run
        page = c.get(web.USAGE_PATH)
        assert page.status_code == 200 and page.headers["x-robots-tag"] == "noindex"
        assert "Usage" in page.text and "Runs per day" in page.text and "people-team" in page.text
        assert "<svg" in page.text and "Show as a table" in page.text
        assert c.get(f"{WEB}?to=usage", follow_redirects=False).headers["location"] == web.USAGE_PATH
        assert "Runs per week" in c.get(web.USAGE_PATH + "?days=90").text
        assert c.get(web.USAGE_PATH + "?days=12").status_code == 200   # an unknown period: the default

        web_run = c.post(WEB, data={"start": "1"}, follow_redirects=False)   # Start still starts a run
        assert web_run.headers["location"].startswith(f"{BASE}/runs/")
    assert rows("run_started")[-1]["person"] == "designer@prezlab.com"
    assert rows("run_started")[-1]["app"] == "Web page"


def test_the_usage_page_is_for_its_owners_only(env, owners):
    with client() as c:
        sign_in(c, "twofa@prezlab.com")
        c.post(WEB, data={"code": "123456"})
        assert "Start with a deck" in c.get(WEB).text and "View usage" not in c.get(WEB).text   # no button
        assert c.get(web.USAGE_PATH).status_code == 403                                        # nor the page
        assert c.get(web.USAGE_PATH + ".csv").status_code == 403


def test_owners_see_a_usage_button_on_the_web_page(env, owners):
    with client() as c:
        sign_in(c, "DESIGNER@prezlab.com")
        start_page = c.get(WEB).text
        assert "Start with a deck" in start_page and f'href="{web.USAGE_PATH}">View usage</a>' in start_page
        assert c.get(web.USAGE_PATH).status_code == 200


def test_the_usage_page_is_off_without_owners(env, fake_odoo, monkeypatch):
    monkeypatch.setattr(config, "USAGE_ADMINS", set())
    assert not usage_page.enabled()
    with client() as c:
        sign_in(c)
        assert c.get(web.USAGE_PATH).status_code == 404


def test_the_csv_holds_the_periods_events_for_excel(env, owners):
    with client() as c:
        start(c)
        call(c, "search_icons", query="=HYPERLINK(1)")
        sign_in(c)
        r = c.get(web.USAGE_PATH + ".csv?days=7")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="prezlab-icons-usage-7-days.csv"' in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    table = list(csv.DictReader(io.StringIO(text[1:])))
    assert [t["event"] for t in table] == ["run_started", "search", "run_started"]   # the owner's sign-in started one
    assert table[1]["query"] == "'=HYPERLINK(1)"                                        # never a live formula
    assert table[0]["date"][2] == "/" and table[0]["date"][5] == "/"                   # DD/MM/YYYY HH:MM
