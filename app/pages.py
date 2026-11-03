"""The connector's few web pages (run pages in connector.py): one look, private by default."""
from starlette.responses import HTMLResponse

# Never cached, indexed, framed or sent on as a referrer: run links carry secrets in their path.
PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
           "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}
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


def render(body: str, status: int = 200, refresh: bool = False) -> HTMLResponse:
    """A page with this body. refresh: it reloads itself every few seconds (a run in progress)."""
    page = PAGE.replace("%REFRESH%", '<meta http-equiv="refresh" content="3">' if refresh else "")
    return HTMLResponse(page.replace("%BODY%", body), status, headers=PRIVATE)
