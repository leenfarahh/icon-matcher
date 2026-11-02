"""Odoo sign-in, two-factor included: a port of Image Studio's src/lib/odoo-totp.ts, so designers sign in to the
connector the way they sign in to Image Studio. Odoo refuses password logins over its RPC API for users with 2FA, so
this drives Odoo's own browser login endpoints, which understand codes:

1. POST /web/session/authenticate. A uid means signed in. No uid but a session cookie means Odoo wants a code.
2. With a trusted-device key kept from an earlier sign-in, GET /web/login/totp carrying it: Odoo finishes the sign-in
   with no code.
3. Otherwise the designer enters a code: read the code page's CSRF token, POST the code with remember=1, and keep the
   trusted-device key Odoo returns for next time (sealed, in oauth.py).
4. Read who signed in, and accept only active internal users with an email on ODOO_ALLOWED_EMAIL_DOMAIN.

This drives pages rather than an API, so an Odoo upgrade can break step 3; every failure comes back as a reason the
sign-in page can explain. The password is never stored or logged."""
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx2

from . import config

log = logging.getLogger("icon-connector.odoo")
TIMEOUT = 20.0
MAX_REDIRECTS = 10
# Odoo turns away login-page requests without a browser-shaped agent.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 "
              "Safari/537.36")
TOTP_FIELD = re.compile(r"""name=["']totp_token["']""", re.I)
TRANSPORT = None   # tests swap in an httpx2.MockTransport standing in for Odoo

MESSAGES = {   # what the sign-in page says; deliberately vague about which half of a credential failed
    "invalid_credentials": "That Odoo login and password combination wasn't recognized.",
    "invalid_totp": "That authentication code wasn't accepted. Codes expire quickly, so try the current one.",
    "inactive_user": "This Odoo account is archived. Contact Operations to reactivate it.",
    "portal_user": "This is an Odoo portal account. Sign in with your Prezlab staff account.",
    "no_email": "Your Odoo account has no email address on it. Ask Operations to add one.",
    "domain_not_allowed": "This account is outside the domain approved for Prezlab Icons.",
    "odoo_unreachable": "Odoo isn't responding right now. Try again in a moment.",
    "totp_unsupported": "Odoo's two-factor step couldn't be completed. Odoo may have changed its login page; "
                        "flag it to Operations.",
    "odoo_error": "Odoo rejected the sign-in request. If this repeats, flag it to Operations.",
}


@dataclass
class Identity:
    uid: int
    login: str
    name: str
    email: str


@dataclass
class Outcome:
    """A signed-in user, or a reason: "totp_required" (pending holds Odoo's half-finished session, for the code
    step) or a key of MESSAGES. detail is for the log only."""
    user: Identity | None = None
    reason: str = ""
    pending: str = ""
    detail: str = ""


class Devices(Protocol):
    """Where trusted-device keys (2FA bypass keys, one per login) are kept between sign-ins."""
    def get(self, login: str) -> str | None: ...
    def put(self, login: str, td_id: str) -> None: ...
    def drop(self, login: str) -> None: ...


class _Fetched:
    def __init__(self, response: httpx2.Response, url: str, text: str):
        self.status, self.url, self.text = response.status_code, url, text
        self.set_cookie = response.headers.get_list("set-cookie")

    def cookie(self, name: str) -> str | None:
        for line in self.set_cookie:   # each Set-Cookie header on its own: values may hold commas
            if m := re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", line):
                return m.group(1)
        return None


async def _fetch(method: str, path: str, cookies: dict | None = None, follow: bool = False, **kw) -> _Fetched:
    """One request to Odoo with exactly these cookies. follow: go through redirects (a page fetch, where Odoo
    redirects between its login pages) with the same cookies, as Image Studio's fetch does; cookies set on the way
    are not picked up. Without follow, Set-Cookie is read from this very response."""
    headers = {"User-Agent": BROWSER_UA, **kw.pop("headers", {})}
    if cookies := {k: v for k, v in (cookies or {}).items() if v}:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    url = config.ODOO_URL + path
    async with httpx2.AsyncClient(transport=TRANSPORT, timeout=TIMEOUT, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            response = await client.request(method, url, headers=headers, **kw)
            if not (follow and response.is_redirect and response.headers.get("location")):
                return _Fetched(response, url, response.text)
            url = urljoin(url, response.headers["location"])
            if response.status_code not in (307, 308):   # like a browser: the next hop is a GET without a body
                method, kw = "GET", {}
                headers.pop("Content-Type", None)
    raise httpx2.TooManyRedirects("Odoo redirected too many times")


def _rpc(params: dict) -> dict:
    return {"json": {"jsonrpc": "2.0", "method": "call", "params": params}}


def _shape(page: _Fetched) -> str:
    """What a page looked like, for the log: its status, path and form fields. Never values or the body."""
    fields = list(dict.fromkeys(re.findall(r"""<input[^>]*?name=["']([^"']+)["']""", page.text, re.I)))[:12]
    return f"{page.status} {urlparse(page.url).path} fields={fields}"


async def _start(login: str, password: str) -> tuple[str, str, int]:
    """Step 1. (kind, session, uid): kind is authenticated, totp_pending, rejected, or error (session holds the
    detail)."""
    try:   # not followed: the session cookie is read from this response
        page = await _fetch("POST", "/web/session/authenticate", **_rpc(
            {"db": config.ODOO_DB, "login": login, "password": password}))
    except httpx2.TimeoutException:
        return "error", "Odoo did not respond in time.", 0
    except httpx2.HTTPError:
        return "error", "Could not reach Odoo.", 0
    session = page.cookie("session_id")
    try:
        payload = json.loads(page.text)
    except ValueError:
        log.warning("odoo sign-in: not JSON: %s", _shape(page))
        return "error", "Odoo returned a non-JSON response to the login request.", 0
    if error := payload.get("error"):
        message = (error.get("data") or {}).get("message") or error.get("message") or ""
        name = (error.get("data") or {}).get("name") or ""
        if re.search(r"access denied|wrong login|credential", message, re.I):   # bad credentials come as a fault
            return "rejected", "", 0
        if session and re.search(r"totp|two.?factor|2fa|mfa", f"{name} {message}", re.I):
            return "totp_pending", session, 0   # some versions ask for the code as a fault
        log.warning("odoo sign-in fault: %s", f"{name}: {message}"[:300])
        return "error", message[:200], 0
    uid = (payload.get("result") or {}).get("uid")
    if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
        if not session:
            return "error", "Odoo authenticated but set no session cookie.", 0
        return "authenticated", session, uid
    return ("totp_pending", session, 0) if session else ("rejected", "", 0)   # no uid: a cookie means "now the code"


def extract_csrf(html: str) -> str | None:
    """The CSRF token on Odoo's code page. Odoo renders it in several shapes across versions, in any attribute order."""
    for pattern in (r"""name=["']csrf_token["'][^>]*?value=["']([^"']+)["']""",
                    r"""value=["']([^"']+)["'][^>]*?name=["']csrf_token["']""",
                    r"""csrf_token\s*[:=]\s*["']([^"']+)["']""",
                    r"""["']csrf_token["']\s*:\s*["']([^"']+)["']""",
                    r"""data-csrf(?:-token)?=["']([^"']+)["']"""):
        if (m := re.search(pattern, html, re.I)) and len(m.group(1)) >= 8:   # a short match latched onto the wrong thing
            return m.group(1)
    return None


async def _trusted(session: str, td_id: str) -> Identity | None:
    """Step 2: finish the sign-in with a trusted-device key. None when Odoo didn't accept it."""
    try:   # not followed: accepting the key rotates the session, and the new one is read from this response
        page = await _fetch("GET", "/web/login/totp", cookies={"session_id": session, "td_id": td_id})
    except httpx2.HTTPError:
        return None
    if 200 <= page.status < 300 and TOTP_FIELD.search(page.text):
        return None   # still asking for a code
    found = await _identity(page.cookie("session_id") or session)   # judged by whether the session has a user
    return found if isinstance(found, Identity) else None


async def _submit_code(session: str, code: str) -> tuple[str, str | None, str]:
    """Step 3. ("", td_id, session after the code) when accepted, else (reason, None, detail)."""
    try:
        page = await _fetch("GET", "/web/login/totp", cookies={"session_id": session}, follow=True)
    except httpx2.HTTPError:
        return "odoo_unreachable", None, ""
    if not TOTP_FIELD.search(page.text):   # landed on /web/login: that page's CSRF token would be refused
        log.warning("odoo code page: not the two-factor page: %s", _shape(page))
        return "totp_unsupported", None, "Odoo did not serve its two-factor page for this session."
    csrf = extract_csrf(page.text)
    if not csrf:
        log.warning("odoo code page: no CSRF token: %s", _shape(page))
        return "totp_unsupported", None, "Could not find a CSRF token on Odoo's two-factor page."
    try:   # not followed: the redirect is how acceptance shows, and td_id is read from this response
        sent = await _fetch("POST", "/web/login/totp", cookies={"session_id": session}, data={
            "csrf_token": csrf, "totp_token": re.sub(r"\s+", "", code), "remember": "1"})
    except httpx2.HTTPError:
        return "odoo_unreachable", None, ""
    after = sent.cookie("session_id") or session   # Odoo issues a new session once the code is accepted
    if 300 <= sent.status < 400:
        return "", sent.cookie("td_id"), after
    if TOTP_FIELD.search(sent.text):   # the form again: wrong code
        return "invalid_totp", None, ""
    log.warning("odoo code submit: unexpected page, treated as accepted: %s", _shape(sent))
    return "", sent.cookie("td_id"), after


async def _identity(session: str) -> Identity | str:
    """Step 4: who the finished session belongs to, or the reason they can't sign in."""
    try:
        page = await _fetch("POST", "/web/session/get_session_info", cookies={"session_id": session}, follow=True,
                            **_rpc({}))
        info = json.loads(page.text)
    except httpx2.HTTPError:
        return "odoo_unreachable"
    except ValueError:
        log.warning("odoo session info: not JSON: %s", _shape(page))
        return "odoo_error"
    result = info.get("result") or {}
    uid = result.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        log.warning("odoo session info: no user (%s)", "an error" if info.get("error") else "no error")
        return "odoo_error"
    record = {}
    try:   # session info has the login but not reliably the email or status: read the user record
        read = await _fetch("POST", "/web/dataset/call_kw", cookies={"session_id": session}, follow=True, **_rpc({
            "model": "res.users", "method": "read", "args": [[uid], ["login", "name", "email", "active", "share"]],
            "kwargs": {}}))
        record = (json.loads(read.text).get("result") or [{}])[0]
    except (httpx2.HTTPError, ValueError, IndexError, AttributeError):
        pass   # fall back to the session info below
    login = str(record.get("login") or result.get("username") or "").strip()
    name = str(record.get("name") or result.get("name") or login).strip()
    email = (record.get("email") if isinstance(record.get("email"), str) else "").strip()
    email = (email or (login if "@" in login else "")).lower()
    if record.get("active") is False:
        return "inactive_user"
    if record.get("share") is True:   # portal and public users: clients, not staff
        return "portal_user"
    if not email:
        return "no_email"
    domain = config.ODOO_ALLOWED_EMAIL_DOMAIN
    if domain and not email.endswith("@" + domain):
        return "domain_not_allowed"
    return Identity(uid, login or email, name or email, email)


async def login(login: str, password: str = "", code: str = "", pending: str = "",
                devices: Devices | None = None) -> Outcome:
    """Sign a designer in. Pass code and pending (from an earlier "totp_required") to finish a two-factor sign-in
    without sending the password again."""
    login = login.strip()
    key = login.lower()
    if not login:
        return Outcome(reason="invalid_credentials")

    def done(found: Identity | str, detail: str = "") -> Outcome:
        return Outcome(user=found) if isinstance(found, Identity) else Outcome(reason=found, detail=detail)

    if pending and code:   # the code step of an earlier attempt
        reason, td_id, session = await _submit_code(pending, code)
        if reason:
            return Outcome(reason=reason, detail=session)
        found = await _identity(session)
        if isinstance(found, Identity) and td_id and devices:   # kept only once the sign-in has really worked
            devices.put(key, td_id)
        return done(found)

    if not password:
        return Outcome(reason="invalid_credentials")
    kind, session, _ = await _start(login, password)
    if kind == "rejected":
        return Outcome(reason="invalid_credentials")
    if kind == "error":
        return Outcome(reason="odoo_unreachable" if re.search(r"reach|respond in time", session) else "odoo_error",
                       detail=session)
    if kind == "authenticated":
        return done(await _identity(session))

    if devices and (td_id := devices.get(key)):   # two-factor: try the trusted device first
        found = await _trusted(session, td_id)
        if found:
            return Outcome(user=found)
        # The attempt used up the pending session, so a code entered against it would fail: forget the key and start
        # a fresh session while the password is still at hand.
        devices.drop(key)
        log.info("odoo trusted device not accepted; asking for a code")
        kind, session, _ = await _start(login, password)
        if kind == "authenticated":   # 2FA was turned off in between
            return done(await _identity(session))
        if kind == "rejected":
            return Outcome(reason="invalid_credentials")
        if kind == "error":
            return Outcome(reason="odoo_error", detail=session)
    return Outcome(reason="totp_required", pending=session)
