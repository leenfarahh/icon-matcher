"""Usage: how the tool is used, kept so its owners can follow adoption and see where it falls short (usage_page.py shows
it at /web/usage). One SQLite file beside the runs (WORK_DIR: the service's disk on Render), so it outlives runs,
restarts and deploys. Recording never gets in a designer's way: a write that fails is logged and dropped.

What is kept: when; which run (its random id); where it started (Claude and which Claude app, or the web page and who
signed in there); the mode; counts (icons placed, PL placeholders found and filled, flags, icons chosen by hand, Claude's
tokens); how long a deck took; library icon ids; and search words that found nothing, cut short. Never a deck's name,
its text, labels or anything else from a slide: client decks stay out of it.

Claude runs carry no person, since the connector has no sign-in. Their Claude app is the name the app gives when it
connects (the MCP handshake, kept in `clients`): newer protocol versions send it with every request; on the handshake
versions Claude connects with today, it is the one app seen connecting with the request's user agent, else that user
agent."""
import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger("icon-connector.usage")
FILE = "usage.db"   # in WORK_DIR, beside the runs (cleanup only deletes run folders)
QUERY_CHARS = 60    # of a search that found nothing
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, event TEXT NOT NULL, run TEXT, source TEXT, app TEXT, person TEXT,
  mode TEXT, data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE TABLE IF NOT EXISTS clients (
  name TEXT NOT NULL, agent TEXT NOT NULL, panel INTEGER NOT NULL, handshakes INTEGER NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (name, agent));
"""
_lock = threading.Lock()   # one writer at a time in this process (the service runs one instance)


def path() -> Path:
    return config.WORK_DIR / FILE


def _db() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=10)
    db.executescript(SCHEMA)
    return db


def clean(value, size: int = 120) -> str:
    """One plain line, whatever a client sends."""
    return re.sub(r"[^\w .:/()+;,=@-]", "?", str(value or ""))[:size].strip()


# --- recording --------------------------------------------------------------------------------------------------

def record(event: str, run: dict | None = None, **data) -> None:
    """One event. run: the run's run.json, for its id, source, Claude app, person and mode (a mode in data wins).
    data: counts, flags and library icon ids only."""
    if not config.USAGE:
        return
    run = run or {}
    mode = data.pop("mode", None) or run.get("mode")
    row = (time.time(), event, run.get("id"), run.get("source"), run.get("app") or None, run.get("person") or None,
           mode, json.dumps(data, separators=(",", ":")))
    try:
        with _lock, closing(_db()) as db, db:
            db.execute("INSERT INTO events (at, event, run, source, app, person, mode, data) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
    except (sqlite3.Error, OSError):
        log.exception("usage: %s not recorded", event)


def saw_client(name: str, agent: str, panel: bool) -> None:
    """A Claude app connected (the MCP handshake): its name, user agent and whether it can show the panel."""
    if not config.USAGE:
        return
    now = time.time()
    try:
        with _lock, closing(_db()) as db, db:
            db.execute("INSERT INTO clients VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT (name, agent) DO UPDATE SET "
                       "handshakes = handshakes + 1, last_seen = excluded.last_seen, panel = excluded.panel",
                       (clean(name) or "?", clean(agent) or "?", int(panel), now, now))
    except (sqlite3.Error, OSError):
        log.exception("usage: a handshake not recorded")


# --- the Claude app behind an MCP request -----------------------------------------------------------------------

_request: contextvars.ContextVar = contextvars.ContextVar("usage_request", default=None)


@contextmanager
def mcp_request(agent: str):
    """Around one MCP request (connector.DeclareApps): its user agent now, and the client name its body gives, which
    the caller fills in once it has read the body (the dict it yields)."""
    holder = {"agent": clean(agent), "client": ""}
    token = _request.set(holder)
    try:
        yield holder
    finally:
        _request.reset(token)


def client_app() -> str:
    """The Claude app making this MCP request, for its run: the name it gives with the request, else the one app seen
    connecting with its user agent, else the user agent's product name."""
    req = _request.get() or {}
    if req.get("client"):
        return req["client"]
    agent = req.get("agent", "")
    if config.USAGE and agent:
        try:
            with closing(_db()) as db:
                names = [n for (n,) in db.execute("SELECT DISTINCT name FROM clients WHERE agent = ?", (agent,))]
            if len(names) == 1:
                return names[0]
        except (sqlite3.Error, OSError):
            log.exception("usage: reading the clients failed")
    return agent.split("/")[0].strip() or "unknown"


# --- Claude's tokens, per deck run ------------------------------------------------------------------------------

class Tokens:
    """Claude's tokens for one deck run. Requests go out from several threads at once (concepts.py), so it adds up
    under a lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts = dict.fromkeys(TOKEN_FIELDS, 0)
        self.requests = 0

    def add(self, usage) -> None:
        with self._lock:
            self.requests += 1
            for f in TOKEN_FIELDS:
                n = getattr(usage, f, 0)
                self.counts[f] += n if isinstance(n, int) and not isinstance(n, bool) else 0


_tokens: contextvars.ContextVar = contextvars.ContextVar("usage_tokens", default=None)


@contextmanager
def counting_tokens():
    """Counts the tokens of every Claude request sent inside it (count_tokens), in this thread and the worker threads
    concepts.py starts with this context."""
    tokens = Tokens()
    token = _tokens.set(tokens)
    try:
        yield tokens
    finally:
        _tokens.reset(token)


def count_tokens(usage) -> None:
    """One Claude reply's usage, towards the deck run that sent it (if one is counting)."""
    tokens = _tokens.get()
    if tokens is not None and usage is not None:
        tokens.add(usage)


# --- what a deck run did ----------------------------------------------------------------------------------------

def deck_run(result: dict) -> dict:
    """A deck run's numbers from its report (report.to_dict), for its event: no text from the deck. A report it can't
    read gives none, never an error for the run."""
    try:
        picks, flags = result.get("picks", []), result.get("flags", [])
        suggest = result.get("mode") == "suggest"
        on_placeholder = lambda x: bool(x["item"].get("placeholder"))
        return {"placed": 0 if suggest else len(picks), "suggested": len(picks) if suggest else 0,
                "placeholders": sum(map(on_placeholder, picks)) + sum(map(on_placeholder, flags)),
                "placeholders_filled": 0 if suggest else sum(map(on_placeholder, picks)),
                "flagged": len(flags), "choosable": sum(1 for f in flags if f.get("spot")),
                "icons": [p["icon"]["id"] for p in picks],
                "claude_trouble": any(str(g).startswith("Claude couldn't") for g in result.get("gaps", []))}
    except (AttributeError, KeyError, TypeError):
        log.exception("usage: a deck run's numbers couldn't be read")
        return {}


# --- reading ----------------------------------------------------------------------------------------------------

def events(since: float, until: float | None = None) -> list[dict]:
    """The events from since (to until), oldest first, each with its data merged in."""
    if not path().exists():
        return []
    with closing(_db()) as db:
        rows = db.execute("SELECT at, event, run, source, app, person, mode, data FROM events WHERE at >= ? AND at < ? "
                          "ORDER BY at, id", (since, until if until is not None else time.time() + 1)).fetchall()
    out = []
    for at, event, run, source, app, person, mode, data in rows:
        try:
            extra = json.loads(data)
        except ValueError:
            extra = {}
        out.append({**(extra if isinstance(extra, dict) else {}), "at": at, "event": event, "run": run,
                    "source": source, "app": app, "person": person, "mode": mode})
    return out


def clients() -> list[dict]:
    """Every Claude app seen connecting, latest first."""
    if not path().exists():
        return []
    with closing(_db()) as db:
        rows = db.execute("SELECT name, agent, panel, handshakes, first_seen, last_seen FROM clients "
                          "ORDER BY last_seen DESC").fetchall()
    return [{"name": n, "agent": a, "panel": bool(p), "handshakes": h, "first_seen": f, "last_seen": last}
            for n, a, p, h, f, last in rows]


def local(at: float) -> datetime:
    """A time in the zone the usage page shows (ICON_USAGE_UTC_OFFSET hours from UTC)."""
    return datetime.fromtimestamp(at, timezone(timedelta(hours=config.USAGE_UTC_OFFSET)))


def zone() -> str:
    h = config.USAGE_UTC_OFFSET
    return "UTC" if not h else f"UTC{'+' if h > 0 else '-'}{abs(h):g}"


def _app(e: dict) -> str:
    return "Web page" if e["source"] == "web" else e["app"] or "unknown"


def summary(days: int, now: float | None = None) -> dict:
    """The usage page's numbers for the last `days` days."""
    now = time.time() if now is None else now
    evs = events(now - days * 86400, now + 1)
    started = [e for e in evs if e["event"] == "run_started"]
    decks = [e for e in evs if e["event"] == "deck_run"]
    ok = [e for e in decks if e.get("ok")]
    added = [e for e in evs if e["event"] == "icons_added"]
    searches = [e for e in evs if e["event"] == "search"]
    ran = {e["run"] for e in ok}
    downloaded = {e["run"] for e in evs if e["event"] == "download"} & ran

    first_run = {}   # run -> its first successful deck run: where the run is counted, in the chart and the tables
    for e in ok:
        first_run.setdefault(e["run"], e)

    by_app = {}
    app = lambda e: by_app.setdefault(_app(e), {"started": 0, "ran": 0, "deck_runs": 0, "downloaded": 0, "placed": 0})
    for e in started:
        app(e)["started"] += 1
    for e in ok:
        app(e)["deck_runs"] += 1
        app(e)["placed"] += e.get("placed", 0)
    for run, e in first_run.items():
        app(e)["ran"] += 1
        app(e)["downloaded"] += run in downloaded

    modes = {}
    for e in ok:
        modes[e["mode"] or "?"] = modes.get(e["mode"] or "?", 0) + 1

    icon_counts = {}
    for e in ok:
        for i in e.get("icons", []) if e["mode"] != "suggest" else []:
            icon_counts[i] = icon_counts.get(i, 0) + 1
    for e in added:
        for i in e.get("icons", []):
            icon_counts[i] = icon_counts.get(i, 0) + 1

    misses = {}
    for e in searches:
        if not e.get("results") and e.get("query"):
            misses[e["query"].lower()] = misses.get(e["query"].lower(), 0) + 1

    people = {}
    for e in started:
        if e["person"]:
            p = people.setdefault(e["person"], {"started": 0, "ran": 0, "last": 0})
            p["started"] += 1
            p["last"] = max(p["last"], e["at"])
    for e in first_run.values():
        if e["person"] in people:
            people[e["person"]]["ran"] += 1

    # The chart: runs by the day (or the week, past a month) of their first deck run, Claude and the web page apart.
    weekly = days > 31
    buckets = {}
    for e in first_run.values():
        d = local(e["at"]).date()
        key = d - timedelta(days=d.weekday()) if weekly else d
        b = buckets.setdefault(key, {"claude": 0, "web": 0})
        b["web" if e["source"] == "web" else "claude"] += 1
    start = local(now - days * 86400).date()
    end = local(now).date()
    if weekly:
        start -= timedelta(days=start.weekday())
    step = timedelta(days=7 if weekly else 1)
    series, d = [], start
    while d <= end:
        series.append({"date": d, **buckets.get(d, {"claude": 0, "web": 0})})
        d += step

    tokens = {f: sum(e.get(f, 0) for e in decks) for f in TOKEN_FIELDS}
    seconds = sorted(e["seconds"] for e in ok if isinstance(e.get("seconds"), (int, float)))
    placeholders = sum(e.get("placeholders", 0) for e in ok if e["mode"] != "suggest")
    return {
        "days": days, "weekly": weekly, "zone": zone(), "since": now - days * 86400,
        "started": len(started), "runs": len(ran), "deck_runs": len(ok), "failed": len(decks) - len(ok),
        "downloaded": len(downloaded),
        "placed": sum(e.get("placed", 0) for e in ok), "chosen": sum(e.get("chosen", 0) for e in added),
        "picked": sum(e.get("picks", 0) for e in added if e["mode"] == "suggest"),
        "uploads": sum(e.get("uploads", 0) for e in added), "warnings": sum(e.get("warnings", 0) for e in added),
        "placeholders": placeholders,
        "placeholders_filled": sum(e.get("placeholders_filled", 0) for e in ok if e["mode"] != "suggest"),
        "attached": sum(1 for e in evs if e["event"] == "deck_attached"),
        "keywords": sum(1 for e in ok if e.get("matching") == "keywords"),
        "claude_trouble": sum(1 for e in ok if e.get("claude_trouble")),
        "searches": len(searches), "tokens": tokens,
        "requests": sum(e.get("requests", 0) for e in decks),
        "median_seconds": seconds[len(seconds) // 2] if seconds else None,
        "by_app": dict(sorted(by_app.items(), key=lambda kv: (-kv[1]["ran"], -kv[1]["started"], kv[0]))),
        "modes": dict(sorted(modes.items(), key=lambda kv: -kv[1])),
        "icons": sorted(icon_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        "misses": sorted(misses.items(), key=lambda kv: (-kv[1], kv[0])),
        "people": dict(sorted(people.items(), key=lambda kv: -kv[1]["last"])),
        "series": series,
    }
