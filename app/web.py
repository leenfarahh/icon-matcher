"""The web page for people without Claude, on the connector's own service (connector.py adds it at /web). They sign in
with their Odoo login, two-factor included (odoo.py), as in Image Studio; only active Prezlab staff accounts get in.
Signed in, each Start opens a new run at its private link, the same page Claude's panel shows. Off unless ODOO_URL and
ODOO_DB are set.

What the page keeps lives in this browser's cookies, sealed with AES-256-GCM under a key only the server holds
(KEY_FILE, made on first use on the service's disk), so there is no sign-in database:
- the sign-in (SESSION_DAYS): who signed in;
- the two-factor step in progress (PENDING_MINUTES): the login and Odoo's half-finished session;
- the trusted device (DEVICE_DAYS): the key Odoo gives a browser that entered a code, so it isn't asked again. Sealed,
  the browser can't read it; and it skips only the code, never the password.
The password goes to Odoo only and is never stored or logged. Deleting KEY_FILE signs every browser out.

Each code step reaches Odoo once (_CodeSteps): a code sent twice, by a second click or a refresh while Odoo answers,
would otherwise fail on the session Odoo retired when it accepted the first, and that failure is what the browser shows.
Like Image Studio's form, the pages also lock their button while a step is on its way (pages.py)."""
import asyncio
import base64
import html
import json
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import auth, config, odoo, pages

log = logging.getLogger("icon-connector.web")
PATH = "/web"
USAGE_PATH = PATH + "/usage"   # the usage page (usage_page.py), under PATH so the sign-in cookie reaches it
TO_USAGE = "usage"             # PATH?to=usage: sign in, then go to the usage page instead of starting a run
KEY_FILE = "web.key"   # in WORK_DIR, beside the runs (cleanup only deletes run folders)
SESSION, PENDING, DEVICE = "pi_session", "pi_pending", "pi_device"
SESSION_DAYS = 30
PENDING_MINUTES = 15
DEVICE_DAYS = 30
REPEAT_SECONDS = 120   # how long a code step that signed in answers a repeat of it
LIMITS = {"ip": (20, 300), "login": (6, 300)}   # sign-in attempts per address and per login, per 5 minutes, as before
_keys: dict = {}


def enabled() -> bool:
    return bool(config.ODOO_URL or config.ODOO_DB)


def check():
    """Refuses settings that would send passwords in the clear or leave the page half set up."""
    if not (config.ODOO_URL and config.ODOO_DB):
        raise RuntimeError("the web page's Odoo sign-in needs both ODOO_URL and ODOO_DB, or neither "
                           "(app/connector-setup.md)")
    for name, value in (("ODOO_URL", config.ODOO_URL), ("ICON_PUBLIC_URL", config.PUBLIC_URL)):
        url = urlparse(value)
        if url.scheme != "https" and url.hostname not in auth.LOCAL_HOSTS:
            raise RuntimeError(f"{name} must be an https address: Odoo passwords travel through it")


# --- sealed cookies ---------------------------------------------------------------------------------------------

def _key() -> bytes:
    """The sealing key, made on first use and kept on the disk, so sign-ins outlast restarts and deploys."""
    path = config.WORK_DIR / KEY_FILE
    if path not in _keys:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
        except FileExistsError:
            pass
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError(f"{path} isn't a sealing key: delete it (every browser then signs in again)")
        _keys[path] = key
    return _keys[path]


def _seal(kind: str, data: dict, seconds: int, bound: str = "") -> str:
    """data, readable only by this server, for this kind of cookie (and login, if bound) until it expires."""
    body = json.dumps({**data, "exp": int(time.time()) + seconds}).encode()
    nonce = os.urandom(12)
    sealed = nonce + AESGCM(_key()).encrypt(nonce, body, f"{kind}\n{bound}".encode())
    return base64.urlsafe_b64encode(sealed).decode().rstrip("=")


def _unseal(kind: str, token: str, bound: str = "") -> dict | None:
    """What _seal sealed, or None when it's missing, forged, for another kind or login, or expired."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], f"{kind}\n{bound}".encode()))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None


def _set(response: Response, name: str, value: str, seconds: int):
    response.set_cookie(name, value, max_age=seconds, path=PATH, httponly=True, samesite="lax",
                        secure=urlparse(config.PUBLIC_URL).scheme == "https")


def _forget(response: Response, name: str):
    response.delete_cookie(name, path=PATH, httponly=True, samesite="lax",
                           secure=urlparse(config.PUBLIC_URL).scheme == "https")


class _BrowserDevice:
    """odoo.Devices for this browser: the trusted-device key Odoo gave it, sealed in its own cookie, for the login it
    was given to. apply() writes any change onto the response."""

    def __init__(self, request: Request):
        self.sealed, self.change = request.cookies.get(DEVICE, ""), None   # change: None as is, "" forget, else new

    def get(self, login: str) -> str | None:
        found = _unseal("device", self.sealed, login)
        return found.get("td") if found else None

    def put(self, login: str, td_id: str):
        self.change = _seal("device", {"td": td_id}, DEVICE_DAYS * 86400, login)

    def drop(self, login: str):
        self.change = ""

    def apply(self, response: Response):
        if self.change:
            _set(response, DEVICE, self.change, DEVICE_DAYS * 86400)
        elif self.change == "":
            _forget(response, DEVICE)


class _Limits:
    """Sign-in attempts per key in a sliding window, in memory (one instance)."""

    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, kind: str, key: str) -> bool:
        limit, window = LIMITS[kind]
        hits, now = self.hits[f"{kind}:{key}"], time.time()
        while hits and hits[0] < now - window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def reset(self, kind: str, key: str):
        self.hits.pop(f"{kind}:{key}", None)


_limits = _Limits()


class _CodeSteps:
    """Each code step once per Odoo session. A repeat waits for the step in progress and, if that one signed in, gets
    its result (for REPEAT_SECONDS). A refused code isn't kept, so the next code entered is tried. In memory (one
    instance)."""

    def __init__(self):
        self.running: dict[str, list] = {}               # Odoo session: [its lock, requests holding or awaiting it]
        self.signed_in: dict[str, tuple[float, tuple]] = {}   # Odoo session: (when, what its step returned)

    async def once(self, session: str, step: Callable) -> tuple:
        """step() returns (odoo.Outcome, anything that goes with it); repeats get the same pair."""
        now = time.time()
        self.signed_in = {k: v for k, v in self.signed_in.items() if v[0] > now - REPEAT_SECONDS}
        entry = self.running.setdefault(session, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                if session in self.signed_in:
                    log.info("web sign-in: a code step sent again; answered with its sign-in")
                    return self.signed_in[session][1]
                result = await step()
                if result[0].user:
                    self.signed_in[session] = (time.time(), result)
                return result
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.running.pop(session, None)


_code_steps = _CodeSteps()


def signed_in(request: Request) -> dict | None:
    """Who signed in on this browser ({"email", "name"}), while the sign-in lasts."""
    return _unseal("session", request.cookies.get(SESSION, ""))


# --- pages ------------------------------------------------------------------------------------------------------

def _problem(message: str) -> str:
    return f'<p class="bad" role="alert">{html.escape(message)}</p>' if message else ""


def _login_page(error: str = "", status: int = 200, note: str = "") -> Response:
    note = f'<p class="note">{html.escape(note)}</p>' if note else ""
    return pages.render(
        "<h1>Sign in</h1><p>Use your Odoo login, the same one you use for Image Studio. No separate password to "
        f"remember.</p>{note}{_problem(error)}"
        '<form method="post"><label for="login">Odoo login</label>'
        '<input type="text" id="login" name="login" inputmode="email" autocomplete="username" autocapitalize="none" '
        'spellcheck="false" placeholder="you@prezlab.com" required autofocus>'
        '<label for="password">Password</label>'
        '<input type="password" id="password" name="password" autocomplete="current-password" required>'
        '<button data-busy="Checking with Odoo…">Sign in</button></form>'
        "<p><small>Your password goes to Odoo only; Prezlab Icons never stores it. Trouble signing in? Your Odoo "
        "account needs an email address on it: contact Operations if it doesn't.</small></p>", status)


def _code_page(login: str, error: str = "", status: int = 200) -> Response:
    return pages.render(
        f"<h1>Enter your code</h1><p>Odoo asks <b>{html.escape(login)}</b> for the 6-digit code from the "
        f"authenticator app you use for Odoo.</p>{_problem(error)}"
        '<form method="post"><label for="code">Authenticator code</label>'
        '<input type="text" id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" '
        'placeholder="123456" required autofocus><button data-busy="Verifying…">Continue</button></form>'
        f"<p><small>After this, this browser won't be asked for a code for {DEVICE_DAYS} days.</small></p>"
        '<form method="post" class="quiet"><input type="hidden" name="restart" value="1"><p><small>Not you? '
        '<button class="link">Start again with a different account</button></small></p></form>', status)


def _start_page(user: dict) -> Response:
    return pages.render(
        f"<h1>Place icons in a deck</h1><p>Signed in as {html.escape(user['name'])}.</p>"
        '<form method="post"><input type="hidden" name="start" value="1">'
        '<button data-busy="Opening…">Start with a deck</button></form>'
        "<p><small>Each start opens a private page for your deck, where you can run it again with changes. The deck "
        "and its copy are deleted as soon as you leave or refresh that page, so download the copy first. In "
        "PowerPoint, save the deck before you upload it.</small></p>"
        f'<form method="post" class="quiet"><input type="hidden" name="signout" value="1"><p><small>Not '
        f'{html.escape(user["email"])}? <button class="link">Sign out</button></small></p></form>')


def _go(link: str) -> Response:
    return RedirectResponse(link, 303, headers=pages.PRIVATE)


def endpoint(start_run: Callable[[str], str]):
    """The page at PATH. start_run(email): opens a new run for whoever signed in and returns its private link
    (connector.py). Only a POST starts one, so a link preview (Teams, Outlook) opening the address never does.
    At PATH?to=usage (where the usage page sends people not signed in), signing in leads to the usage page instead;
    the forms post back to the same address, so it holds through the code step."""
    async def web(request: Request) -> Response:
        user = signed_in(request)
        pending = _unseal("pending", request.cookies.get(PENDING, ""))
        to_usage = request.query_params.get("to") == TO_USAGE
        if request.method == "GET":
            if user and to_usage:
                return _go(USAGE_PATH)
            return _start_page(user) if user else _code_page(pending["login"]) if pending else _login_page()
        form = await request.form(max_files=0, max_fields=4)
        if "signout" in form:
            response = _login_page(note="You're signed out.")
            _forget(response, SESSION)
            return response
        if "start" in form:
            return _go(start_run(user["email"])) if user else _login_page("Sign in first.", 403)
        if "restart" in form:
            response = _login_page()
            _forget(response, PENDING)
            return response
        return await _sign_in(request, form, pending, (lambda email: USAGE_PATH) if to_usage else start_run)
    return web


async def _sign_in(request: Request, form, pending: dict | None, start_run: Callable[[str], str]) -> Response:
    """The Odoo login, then the code if Odoo asks for one (without sending the password again); signed in, straight
    to a new run (or where start_run leads: the usage page)."""
    code_step = bool(pending) and "code" in form   # Odoo has the password and waits for a code
    login = (pending["login"] if code_step else str(form.get("login") or "")).strip()
    password, code = str(form.get("password") or ""), str(form.get("code") or "").strip()

    def again(message: str, status: int, at_code: bool = code_step) -> Response:
        return _code_page(login, message, status) if at_code else _login_page(message, status)

    if not login or not (code if code_step else password):
        return again("Enter your code." if code_step else "Enter your Odoo login and password.", 400)
    if not _limits.allow("ip", request.client.host if request.client else "?"):
        return again("Too many sign-in attempts from this network. Try again in a few minutes.", 429)
    if not _limits.allow("login", login.lower()):
        return again("Too many attempts for this account. Wait a few minutes before trying again.", 429)

    device = _BrowserDevice(request)

    async def step() -> tuple:
        found = await odoo.login(login, password, code, pending["session"] if code_step else "", device)
        return found, device.change   # the trusted device goes with the sign-in, to a repeat too

    try:
        if code_step:
            outcome, device.change = await _code_steps.once(pending["session"], step)
        else:
            outcome, _ = await step()
    except Exception:
        log.exception("web sign-in: unexpected failure talking to Odoo")
        return again("Sign-in isn't available right now. Try again shortly.", 502)
    if outcome.reason == "totp_required":
        response = _code_page(login)
        _set(response, PENDING, _seal("pending", {"login": login, "session": outcome.pending}, PENDING_MINUTES * 60),
             PENDING_MINUTES * 60)
    elif outcome.user is None:
        if outcome.detail:
            log.warning("web sign-in refused (%s): %s", outcome.reason, outcome.detail)
        keep = code_step and outcome.reason == "invalid_totp"   # a wrong code can be retried; else start again
        status = 502 if outcome.reason in ("odoo_unreachable", "odoo_error") else 401
        response = again(odoo.MESSAGES.get(outcome.reason, odoo.MESSAGES["odoo_error"]), status, keep)
        if code_step and not keep:
            _forget(response, PENDING)
    else:
        _limits.reset("login", login.lower())
        user = outcome.user
        log.info("signed in to the web page: %s", user.email)
        response = _go(start_run(user.email))
        _set(response, SESSION, _seal("session", {"email": user.email, "name": user.name}, SESSION_DAYS * 86400),
             SESSION_DAYS * 86400)
        _forget(response, PENDING)
    device.apply(response)
    return response
