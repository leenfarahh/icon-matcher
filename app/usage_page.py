"""The usage page at /web/usage (web.USAGE_PATH): what usage.py recorded, for the tool's owners. Only the people in
ICON_USAGE_ADMINS see it, signed in with Odoo on the web page (web.py); anyone else signed in is turned away, and
someone not signed in is sent to sign in first. Off without the web page or without anyone in ICON_USAGE_ADMINS.
The CSV export (/web/usage.csv) holds the same period's events, one per row, for Excel."""
import csv
import html
import io
import math
import time

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import config, pages, usage, web

PERIODS = (7, 30, 90, 365)   # days the page can show
DEFAULT_DAYS = 30
CLAUDE, WEB = "#00899e", "#eb6834"   # the chart's two series; checked for color-vision deficiency and contrast on white
TOP = 12                             # rows in the icon and search tables


def enabled() -> bool:
    return web.enabled() and bool(config.USAGE_ADMINS)


def _admin(request: Request) -> tuple[dict | None, Response | None]:
    """(who, None) for an owner signed in; else (None, where they go instead)."""
    user = web.signed_in(request)
    if user is None:
        return None, RedirectResponse(f"{web.PATH}?to={web.TO_USAGE}", 303, headers=pages.PRIVATE)
    if not web.is_usage_admin(user):
        return None, pages.render(
            f"<h1>Usage is for the tool's owners</h1><p>You're signed in as {html.escape(user['email'])}, which isn't "
            "on the list for this page. The tool's owner can add you (ICON_USAGE_ADMINS).</p>"
            f'<a class="button" href="{web.PATH}">Back to Prezlab Icons</a>', 403)
    return user, None


def _days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", DEFAULT_DAYS))
    except ValueError:
        return DEFAULT_DAYS
    return days if days in PERIODS else DEFAULT_DAYS


# --- formatting -------------------------------------------------------------------------------------------------

e = html.escape


def _n(value) -> str:
    return f"{value:,}"


def _compact(value: int) -> str:
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + unit
    return _n(value)


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "n/a"


def _date(at: float) -> str:
    return usage.local(at).strftime("%d/%m/%Y")


def _table(head: list[str], rows: list[list], numeric: set[int], empty: str) -> str:
    """A table, numbers right-aligned in tabular figures; `empty` in its place when there are no rows. Cells are
    escaped here."""
    if not rows:
        return f'<p class="empty">{e(empty)}</p>'
    cls = lambda k: ' class="num"' if k in numeric else ""
    th = "".join(f"<th{cls(k)}>{e(h)}</th>" for k, h in enumerate(head))
    body = "".join("<tr>" + "".join(f"<td{cls(k)}>{e(str(v))}</td>" for k, v in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _tile(label: str, value: str, note: str) -> str:
    return (f'<div class="tile"><p class="label">{e(label)}</p><p class="value">{e(value)}</p>'
            f'<p class="note">{e(note)}</p></div>')


# --- the chart: runs per day (or week), Claude and the web page stacked ------------------------------------------

W, H, LEFT, BOTTOM, TOP_PAD, RIGHT = 720, 210, 40, 26, 10, 8


def _ticks(peak: int) -> tuple[int, int]:
    """(step, top) for at most four clean steps: 1, 2 or 5 times a power of ten."""
    peak = max(peak, 1)
    for k in range(0, 7):
        for m in (1, 2, 5):
            step = m * 10 ** k
            if math.ceil(peak / step) <= 4:
                return step, step * math.ceil(peak / step)
    return peak, peak


def _column(x: float, base: float, h: float, w: float, color: str, rounded: bool) -> str:
    """A column segment from base up by h: its top corners rounded (4px) when it's the column's data end, square at
    the baseline."""
    if h <= 0:
        return ""
    r = min(4.0, w / 2, h) if rounded else 0
    top = base - h
    return (f'<path fill="{color}" d="M{x:.1f},{base:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} '
            f'H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{base:.1f} Z"/>')


def _chart(s: dict) -> str:
    series = s["series"]
    unit = "week" if s["weekly"] else "day"
    label = lambda p: p["date"].strftime("%d/%m")
    step, top = _ticks(max((p["claude"] + p["web"] for p in series), default=0))
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP_PAD - BOTTOM
    base = TOP_PAD + plot_h
    band = plot_w / max(len(series), 1)
    bar = min(24.0, band * 0.62)
    y = lambda v: plot_h * v / top
    out = []
    for v in range(0, top + 1, step):   # hairline grid, solid; the value on the left
        gy = base - y(v)
        out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
                   f'<text x="{LEFT - 8}" y="{gy + 4:.1f}" text-anchor="end">{_n(v)}</text>')
    every = max(1, math.ceil(len(series) / 8))
    for k, p in enumerate(series):
        x0 = LEFT + k * band
        x = x0 + (band - bar) / 2
        hc, hw = y(p["claude"]), y(p["web"])
        gap = 2 if hc and hw else 0   # the surface gap between the two segments
        out.append(_column(x, base, hc, bar, CLAUDE, rounded=not hw))
        out.append(_column(x, base - hc - gap, hw - gap if gap else hw, bar, WEB, rounded=True))
        tip = (f"{'Week of ' if s['weekly'] else ''}{p['date'].strftime('%d/%m/%Y')}: "
               f"{p['claude']} Claude, {p['web']} web page")
        out.append(f'<rect x="{x0:.1f}" y="{TOP_PAD}" width="{band:.1f}" height="{plot_h}" class="hit">'
                   f"<title>{e(tip)}</title></rect>")
        if k % every == 0:
            out.append(f'<text x="{x0 + band / 2:.1f}" y="{H - 8}" text-anchor="middle">{e(label(p))}</text>')
    total = sum(p["claude"] + p["web"] for p in series)
    svg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Runs per {unit}: {total} in this period, Claude and '
           f'the web page stacked">{"".join(out)}</svg>')
    rows = [[("Week of " if s["weekly"] else "") + p["date"].strftime("%d/%m/%Y"), p["claude"], p["web"]]
            for p in series if p["claude"] or p["web"]]
    legend = (f'<div class="legend"><span><i style="background:{CLAUDE}"></i>Claude</span>'
              f'<span><i style="background:{WEB}"></i>Web page</span></div>')
    return (f'<section class="card"><h2>Runs per {unit}</h2><p class="hint">Runs counted on the {unit} of their first '
            f'deck run{", weeks starting Monday" if s["weekly"] else ""}. Hover a column for its numbers.</p>'
            f'{legend}<div class="chart">{svg}</div>'
            f'<details><summary>Show as a table</summary>'
            f'{_table([unit.title(), "Claude", "Web page"], rows, {1, 2}, "No runs in this period.")}</details></section>')


# --- the page ---------------------------------------------------------------------------------------------------

CSS = """%BRAND_CSS%
:root { --hair: #e3e9ea; --muted: #4d6c74; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--shell); color: var(--teal); font: 500 14px/1.45 var(--font);
  letter-spacing: -0.01em; -webkit-font-smoothing: antialiased; }
a { color: inherit; text-underline-offset: 2px; }
:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
.top { background: var(--teal); color: var(--shell); }
.top .in { max-width: 1120px; margin: 0 auto; padding: 16px 24px; display: flex; align-items: center;
  justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.top .wordmark { display: block; width: 98px; height: 22px; color: var(--shell); }
.top .who { margin: 0; font-size: 13px; color: rgba(182, 237, 243, .85); }
main { max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }
.head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { margin: 0; font-size: 28px; font-weight: 700; line-height: 1; letter-spacing: -0.03em; }
.sub { margin: 8px 0 0; color: var(--muted); }
.controls { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.seg { display: inline-flex; padding: 3px; background: #fff; border: 1px solid var(--hair); border-radius: 999px; }
.seg a { padding: 6px 12px; border-radius: 999px; color: var(--muted); font-size: 13px; font-weight: 600;
  text-decoration: none; }
.seg a[aria-current] { background: var(--teal); color: var(--shell); }
.csv { font-size: 13px; font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 24px; }
.tile, .card { background: #fff; border: 1px solid var(--hair); border-radius: 16px; }
.tile { padding: 16px 18px; }
.tile p { margin: 0; }
.tile .label { font-size: 12px; font-weight: 600; color: var(--muted); }
.tile .value { margin-top: 6px; font-size: 30px; font-weight: 700; line-height: 1.05; letter-spacing: -0.03em; }
.tile .note { margin-top: 6px; font-size: 12px; color: var(--muted); }
.card { margin-top: 16px; padding: 20px 22px; min-width: 0; }
.card h2 { margin: 0; font-size: 16px; font-weight: 700; letter-spacing: -0.02em; }
.hint { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; margin-top: 16px; }
.grid .card { margin-top: 0; }
.legend { display: flex; gap: 16px; margin-top: 12px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 3px;
  vertical-align: -1px; }
.chart { margin-top: 8px; overflow-x: auto; }
.chart svg { display: block; width: 100%; min-width: 540px; height: auto; font: 500 11px var(--font);
  font-variant-numeric: tabular-nums; }
.chart text { fill: var(--muted); }
.chart .grid { stroke: var(--hair); stroke-width: 1; }
.chart .hit { fill: transparent; }
.chart .hit:hover { fill: rgba(0, 37, 40, .04); }
details { margin-top: 12px; }
summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--muted); }
.scroll { overflow-x: auto; }
table { width: 100%; margin-top: 12px; border-collapse: collapse; font-size: 13px; }
th { padding: 0 10px 8px 0; border-bottom: 1px solid var(--hair); color: var(--muted); font-size: 11px;
  font-weight: 600; letter-spacing: .06em; text-align: left; text-transform: uppercase; white-space: nowrap; }
td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--hair); vertical-align: top; overflow-wrap: break-word; }
tr:last-child td { border-bottom: 0; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; overflow-wrap: normal; }
.empty { margin: 12px 0 0; color: var(--muted); }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin: 14px 0 0; }
.facts div { margin: 0; }
.facts dt { font-size: 12px; font-weight: 600; color: var(--muted); }
.facts dd { margin: 4px 0 0; font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
@media (max-width: 600px) {
  .top .in { padding: 14px 16px; }
  main { padding: 24px 16px 48px; }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .tile { padding: 14px; }
  .tile .label { min-height: 2.9em; }   /* two lines everywhere, so the values in a row line up */
  .tile .value { font-size: 26px; }
  .grid { grid-template-columns: 1fr; }
  .card { padding: 18px 16px; }
}"""


def _render(user: dict, s: dict) -> str:
    days = s["days"]
    seg = "".join(f'<a href="?days={d}"{" aria-current=page" if d == days else ""}>{d} days</a>' for d in PERIODS)
    placeholders, filled = s["placeholders"], s["placeholders_filled"]
    by_hand = s["chosen"] + s["picked"]
    tiles = "".join([
        _tile("Runs", _n(s["runs"]), f"{_n(s['started'])} started, {_n(s['deck_runs'])} deck runs incl. re-runs"),
        _tile("Delivered", _pct(s["downloaded"], s["runs"]), f"{_n(s['downloaded'])} of {_n(s['runs'])} runs downloaded"),
        _tile("Placeholders filled automatically", _pct(filled, placeholders),
              f"{_n(filled)} of {_n(placeholders)} PL placeholders"),
        _tile("Icons placed", _n(s["placed"] + by_hand), f"{_n(s['placed'])} by the tool, {_n(by_hand)} chosen by designers"),
        _tile("Failed deck runs", _n(s["failed"]), "files that couldn't be read, or errors"),
        _tile("Web page users", _n(len(s["people"])), "Claude runs have no sign-in"),
    ])
    apps = _table(["Started from", "Started", "Ran a deck", "Downloaded", "Icons placed"],
                  [[a, _n(r["started"]), _n(r["ran"]), _n(r["downloaded"]), _n(r["placed"])]
                   for a, r in s["by_app"].items()], {1, 2, 3, 4}, "No runs in this period.")
    modes = _table(["Mode", "Deck runs"], [[m, _n(n)] for m, n in s["modes"].items()], {1}, "No deck runs yet.")
    hand = (f'<dl class="facts"><div><dt>Chosen by hand</dt><dd>{_n(s["chosen"])}</dd></div>'
            f'<div><dt>Designers\' own uploads</dt><dd>{_n(s["uploads"])}</dd></div>'
            f'<div><dt>Reuse warnings</dt><dd>{_n(s["warnings"])}</dd></div>'
            f'<div><dt>Decks sent from the chat</dt><dd>{_n(s["attached"])}</dd></div></dl>')
    icons = _table(["Icon", "Times placed"], [[i, _n(n)] for i, n in s["icons"][:TOP]], {1},
                   "No icons placed in this period.")
    misses = _table(["Searched for", "Times"], [[q, _n(n)] for q, n in s["misses"][:TOP]], {1},
                    "Every search in Claude found icons.")
    people = _table(["Person", "Started", "Ran a deck", "Last used"],
                    [[p, _n(r["started"]), _n(r["ran"]), _date(r["last"])] for p, r in s["people"].items()],
                    {1, 2, 3}, "Nobody used the web page in this period.")
    clients = _table(["Claude app", "User agent", "Panel", "Handshakes", "Last seen"],
                     [[c["name"], c["agent"], "yes" if c["panel"] else "no", _n(c["handshakes"]), _date(c["last_seen"])]
                      for c in usage.clients()], {3, 4}, "No Claude app has connected yet.")
    t = s["tokens"]
    tokens_in = t["input_tokens"] + t["cache_creation_input_tokens"] + t["cache_read_input_tokens"]
    per_run = lambda n: _compact(round(n / s["deck_runs"])) if s["deck_runs"] else "n/a"
    median = f"{s['median_seconds']:g} s" if s["median_seconds"] is not None else "n/a"
    claude = (f'<dl class="facts"><div><dt>Requests</dt><dd>{_n(s["requests"])}</dd></div>'
              f'<div><dt>Input tokens</dt><dd>{_compact(tokens_in)}</dd></div>'
              f'<div><dt>Of which cache reads</dt><dd>{_compact(t["cache_read_input_tokens"])}</dd></div>'
              f'<div><dt>Output tokens</dt><dd>{_compact(t["output_tokens"])}</dd></div>'
              f'<div><dt>Tokens per deck run</dt><dd>{per_run(tokens_in + t["output_tokens"])}</dd></div>'
              f'<div><dt>Median deck time</dt><dd>{e(median)}</dd></div>'
              f'<div><dt>Keyword matching only</dt><dd>{_n(s["keywords"])}</dd></div>'
              f'<div><dt>Claude had trouble</dt><dd>{_n(s["claude_trouble"])}</dd></div></dl>')
    body = f"""<header class="top"><div class="in">{pages.WORDMARK}
<p class="who">Signed in as {e(user['name'])} · <a href="{web.PATH}">Prezlab Icons</a></p></div></header>
<main>
<div class="head"><div><h1>Usage</h1>
<p class="sub">Last {days} days, dates in {e(s['zone'])}. Claude runs have no sign-in, so they're counted by Claude app.
No deck content is kept.</p></div>
<div class="controls"><nav class="seg" aria-label="Period">{seg}</nav>
<a class="csv" href="{web.USAGE_PATH}.csv?days={days}">Download CSV</a></div></div>
<section class="tiles" aria-label="Summary">{tiles}</section>
{_chart(s)}
<div class="grid">
<section class="card"><h2>Where runs come from</h2><p class="hint">Claude runs by the app that started them.</p>{apps}</section>
<section class="card"><h2>Modes and choices</h2><p class="hint">Deck runs by mode, and what designers added by hand.</p>{modes}{hand}</section>
</div>
<div class="grid">
<section class="card"><h2>Most used icons</h2><p class="hint">Placed by the tool or chosen by designers.</p>{icons}</section>
<section class="card"><h2>Searches that found nothing</h2><p class="hint">Searches in Claude: candidates for new icons.</p>{misses}</section>
</div>
<div class="grid">
<section class="card"><h2>Web page users</h2><p class="hint">People signed in with Odoo.</p>{people}</section>
<section class="card"><h2>Claude apps connecting</h2><p class="hint">All time, from each connection's handshake.</p>{clients}</section>
</div>
<section class="card"><h2>Claude API</h2><p class="hint">What picking icons used, across every deck run in the period.</p>{claude}</section>
</main>"""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" "
            f"content=\"noindex\"><title>Prezlab Icons usage</title>{pages.TAB_ICON}"
            f"<style>{CSS.replace('%BRAND_CSS%', pages.BRAND_CSS)}</style></head><body>{body}</body></html>")


async def page(request: Request) -> Response:
    user, away = _admin(request)
    if away is not None:
        return away
    return Response(_render(user, usage.summary(_days(request))), media_type="text/html", headers=pages.PRIVATE)


# --- the CSV export ---------------------------------------------------------------------------------------------

FIRST = ["date", "event", "run", "source", "app", "person", "mode"]


def _cell(value) -> str:
    """A CSV cell Excel reads as text: lists joined, and anything that could start a formula quoted."""
    text = " ".join(map(str, value)) if isinstance(value, list) else "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def export(request: Request) -> Response:
    user, away = _admin(request)
    if away is not None:
        return away
    days = _days(request)
    events = usage.events(time.time() - days * 86400)
    extra = sorted({k for ev in events for k in ev} - set(FIRST) - {"at"})
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(FIRST + extra)
    for ev in events:
        writer.writerow([usage.local(ev["at"]).strftime("%d/%m/%Y %H:%M")]
                        + [_cell(ev.get(k)) for k in FIRST[1:] + extra])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        **pages.PRIVATE, "Content-Disposition": f'attachment; filename="prezlab-icons-usage-{days}-days.csv"'})
