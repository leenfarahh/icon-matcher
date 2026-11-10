"""The connector's few web pages (the web page's sign-in and start pages in web.py, expired links in connector.py): one
look, private by default. Laid out like Image Studio's sign-in, on the Prezlab Brand Kit (static/brand/README.md): a
Dark Teal panel carrying the brand, an Off-White panel carrying the work."""
from pathlib import Path

from starlette.responses import HTMLResponse

# Never cached, indexed, framed or sent on as a referrer: run links carry secrets in their path.
PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
           "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}
# The "pl:" mark in the browser tab, as Image Studio's (static/brand/README.md). static/index.html carries the same line.
TAB_ICON = '<link rel="icon" type="image/png" href="/favicon.png">'
WORDMARK = ((Path(__file__).parent / "static" / "brand" / "prezlab-wordmark.svg").read_text(encoding="utf-8").strip()
            .replace('<svg xmlns="http://www.w3.org/2000/svg" ', '<svg class="wordmark" ', 1))
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">%REFRESH%<title>Prezlab Icons</title>%TAB_ICON%<style>
@font-face { font-family: "Figtree"; src: url("/fonts/Figtree-latin.woff2") format("woff2"); font-weight: 300 900;
  font-display: swap; unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304,
  U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }
@font-face { font-family: "Figtree"; src: url("/fonts/Figtree-latin-ext.woff2") format("woff2"); font-weight: 300 900;
  font-display: swap; unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308,
  U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C4, U+2113, U+2C60-2C7F, U+A720-A7FF; }
:root { --teal: #002528; --teal-700: #0a3336; --slate: #62848c; --mint: #b6edf3; --shell: #f4f6f6; --alert: #ff7c4a;
  --line: rgba(98, 132, 140, .35); --font: "Figtree", system-ui, -apple-system, "Segoe UI", sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  background: var(--shell); color: var(--teal); font: 500 15px/1.4 var(--font); letter-spacing: -0.01em;
  -webkit-font-smoothing: antialiased; }
::selection { background: var(--mint); color: var(--teal); }
:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
.brand { position: relative; overflow: hidden; display: flex; flex-direction: column; justify-content: space-between;
  gap: 48px; padding: 40px 56px; background: var(--teal); color: var(--shell); }
.brand .wordmark { display: block; width: 113px; height: 26px; color: var(--shell); }
.brand .pitch { position: relative; z-index: 1; max-width: 30rem; }
.eyebrow { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
  color: rgba(182, 237, 243, .75); }
.brand h2 { margin: 18px 0 0; font-size: 44px; font-weight: 700; line-height: 1; letter-spacing: -0.03em; }
.brand .pitch p:last-child { margin: 20px 0 0; color: rgba(182, 237, 243, .85); max-width: 26rem; }
.brand .fine { position: relative; z-index: 1; margin: 0; font-size: 12px; color: rgba(244, 246, 246, .6); }
.motif { position: absolute; right: -56px; top: 50%; transform: translateY(-50%); display: grid; gap: 20px; width: 200px; }
.motif span { display: block; aspect-ratio: 1; border-radius: 50%; background: var(--mint); }
.work { display: flex; align-items: center; justify-content: center; padding: 56px 24px; }
.inner { width: 100%; max-width: 380px; }
h1 { margin: 0; font-size: 26px; font-weight: 700; line-height: 1; letter-spacing: -0.03em; }
p { margin: 12px 0 0; } .inner > p, small { color: var(--slate); } small { font-size: 12px; }
.bad { margin: 16px 0 0; padding: 10px 14px; border-left: 3px solid var(--alert); border-radius: 10px;
  background: rgba(255, 124, 74, .14); color: var(--teal); }
.note { margin: 16px 0 0; padding: 10px 14px; border-left: 3px solid var(--mint); border-radius: 10px;
  background: rgba(182, 237, 243, .4); color: var(--teal); }
form { margin: 24px 0 0; }
label { display: block; margin: 16px 0 6px; font-size: 11px; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; color: var(--slate); }
input[type=text], input[type=password] { display: block; width: 100%; padding: 12px 14px; font: inherit;
  letter-spacing: inherit; color: var(--teal); background: #fff; border: 1px solid var(--line); border-radius: 12px; }
input:focus { border-color: var(--teal); outline: none; }
input::placeholder { color: var(--slate); opacity: .7; }
.button, button { display: inline-flex; align-items: center; justify-content: center; width: 100%; margin-top: 24px;
  padding: 12px 20px; border: 0; border-radius: 999px; background: var(--teal); color: var(--shell); font: inherit;
  font-weight: 600; letter-spacing: -0.02em; text-decoration: none; cursor: pointer; }
.button:hover, button:hover { background: var(--teal-700); }
button:disabled, button:disabled:hover { background: var(--teal-700); opacity: .75; cursor: progress; }
button.link { display: inline; width: auto; margin: 0; padding: 0; background: none; color: inherit; font-weight: 500;
  letter-spacing: inherit; text-decoration: underline; text-underline-offset: 2px; }
button.link:hover { background: none; color: var(--teal); }
form.quiet { margin: 0; }
.inner > form + p, .inner > p + form.quiet { margin-top: 24px; }
@media (max-width: 860px) {
  body { grid-template-columns: 1fr; }
  .brand { gap: 28px; padding: 28px 24px; }
  .brand h2 { font-size: 30px; }
  .motif { display: none; }
  .work { align-items: flex-start; padding: 40px 16px; }
}
</style></head><body>
<section class="brand">%WORDMARK%
<div class="pitch"><p class="eyebrow">Prezlab Icons</p><h2>Approved icons, placed in your deck.</h2>
<p>Upload a PowerPoint deck and get a new copy with icons from Prezlab's library. Your original file is never
changed.</p></div>
<p class="fine">Internal tool. Treat every deck as client-confidential.</p>
<div class="motif" aria-hidden="true"><span></span><span></span></div></section>
<main class="work"><div class="inner">%BODY%</div></main>
<script>
// Each form goes once, its button saying what's happening, as in Image Studio: a second click while Odoo answers would
// send the same step twice. Back to a page the browser kept, its forms work again.
document.addEventListener("submit", function (event) {
  var form = event.target, button = form.querySelector("button[data-busy]");
  if (form.dataset.sent) { event.preventDefault(); return; }
  form.dataset.sent = "1";
  if (button) { button.dataset.label = button.textContent; button.textContent = button.dataset.busy; button.disabled = true; }
});
addEventListener("pageshow", function () {
  document.querySelectorAll("form[data-sent]").forEach(function (form) {
    var button = form.querySelector("button[data-busy]");
    delete form.dataset.sent;
    if (button && button.dataset.label) { button.textContent = button.dataset.label; button.disabled = false; }
  });
});
</script></body></html>""".replace("%WORDMARK%", WORDMARK).replace("%TAB_ICON%", TAB_ICON)


def render(body: str, status: int = 200, refresh: bool = False) -> HTMLResponse:
    """A page with this body. refresh: it reloads itself every few seconds (a run in progress)."""
    page = PAGE.replace("%REFRESH%", '<meta http-equiv="refresh" content="3">' if refresh else "")
    return HTMLResponse(page.replace("%BODY%", body), status, headers=PRIVATE)
