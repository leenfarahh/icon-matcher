"""Sign-in for the Claude connector, built like Image Studio's: the connector is its own OAuth server, and designers
prove who they are with their Odoo login (odoo.py). It needs no domain and no IT setup, so it runs on the service's
own onrender.com address.

The MCP SDK serves the OAuth endpoints (/register, /authorize, /token, /revoke and the discovery documents); this module
keeps what they hand it (Provider) and adds the sign-in page they send designers to:
1. Claude registers itself the first time (only Claude's own return addresses are accepted) and opens /authorize.
2. /authorize checks the request and sends the designer to /signin/<request>, a page naming who is asking.
3. Once Odoo accepts their login (and code, with 2FA), the page sends them back to Claude with a one-time code.
4. Claude trades the code and its PKCE verifier at /token for a one-hour access token and a refresh token.

Codes and tokens are kept as SHA-256 hashes, never as issued. Refresh tokens rotate, and presenting a used one revokes
that sign-in's tokens. A sign-in lasts SIGN_IN_DAYS, after which Claude sends the designer through the page again.
Everything lives in one SQLite file (ICON_AUTH_DB) on the service's disk."""
import base64
import hashlib
import html
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import odoo, pages

log = logging.getLogger("icon-connector.oauth")
SCOPE = "icons"
ACCESS_SECONDS = 3600
SIGN_IN_DAYS = 30
CODE_SECONDS = 120           # Claude redeems the code right away
REQUEST_SECONDS = 15 * 60    # time to fill in the sign-in page
TRUSTED_DEVICE_DAYS = 30
CLAUDE_HOSTS = ("claude.ai", "claude.com")
LIMITS = {"ip": (20, 300), "login": (6, 300)}   # sign-in attempts per IP and per login, per 5 minutes

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    login TEXT, odoo_session TEXT, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, grant_id TEXT NOT NULL, data TEXT NOT NULL,
    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
    grant_id TEXT NOT NULL, subject TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, expires INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens (grant_id);
CREATE TABLE IF NOT EXISTS devices (login TEXT PRIMARY KEY, sealed TEXT NOT NULL, expires REAL NOT NULL);
"""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def allowed_redirect(uri: str) -> bool:
    """Where a sign-in may send the designer back to: Claude (claude.ai, claude.com and their subdomains, which the
    PowerPoint add-in uses), or this machine for Claude Code. Anything else could hand a designer's sign-in to a
    stranger."""
    url = urlparse(uri)
    host = (url.hostname or "").lower()
    if url.fragment:
        return False
    if url.scheme == "https":
        return any(host == h or host.endswith("." + h) for h in CLAUDE_HOSTS)
    return url.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")


def sealing_key(secret: str) -> bytes | None:
    """The AES-256 key for trusted-device keys, from ODOO_TD_ENCRYPTION_KEY (base64 of 32 bytes, or a passphrase of
    32+ characters, as in Image Studio). None when unset: designers with 2FA then enter a code at every sign-in."""
    secret = secret.strip()
    if not secret:
        return None
    try:
        raw = base64.b64decode(secret, validate=True)
        if len(raw) == 32:
            return raw
    except ValueError:
        pass
    if len(secret) >= 32:
        return hashlib.sha256(secret.encode()).digest()
    raise ValueError("ODOO_TD_ENCRYPTION_KEY must be base64 of 32 bytes, or at least 32 characters")


class Store:
    """The SQLite file, shared by the request threads."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def run(self, sql: str, *args) -> int:
        """Run a change; the number of rows it touched."""
        with self.lock:
            return self.db.execute(sql, args).rowcount

    def row(self, sql: str, *args) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, args).fetchone()


class TrustedDevices:
    """Odoo trusted-device keys (2FA bypass keys), sealed with AES-256-GCM so the file alone is no use."""

    def __init__(self, store: Store, key: bytes | None):
        self.store, self.aes = store, AESGCM(key) if key else None

    def get(self, login: str) -> str | None:
        row = self.store.row("SELECT sealed FROM devices WHERE login = ? AND expires > ?", login, time.time())
        if not (row and self.aes):
            return None
        try:
            nonce, sealed = (base64.urlsafe_b64decode(p) for p in row["sealed"].split("."))
            return self.aes.decrypt(nonce, sealed, login.encode()).decode()
        except Exception:   # sealed under another key: ask for a code instead
            return None

    def put(self, login: str, td_id: str):
        if self.aes:
            nonce = os.urandom(12)
            sealed = ".".join(base64.urlsafe_b64encode(p).decode()
                              for p in (nonce, self.aes.encrypt(nonce, td_id.encode(), login.encode())))
            self.store.run("INSERT OR REPLACE INTO devices VALUES (?, ?, ?)", login, sealed,
                           time.time() + TRUSTED_DEVICE_DAYS * 86400)

    def drop(self, login: str):
        self.store.run("DELETE FROM devices WHERE login = ?", login)


class Code(AuthorizationCode):
    grant_id: str


class Refresh(RefreshToken):
    grant_id: str


class Limits:
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


class Provider:
    """What the SDK's OAuth endpoints call (mcp.server.auth.provider.OAuthAuthorizationServerProvider), and the
    sign-in page."""

    def __init__(self, store: Store, issuer: str, mcp_url: str, sealing: bytes | None = None):
        self.store, self.issuer, self.mcp_url = store, issuer.rstrip("/"), mcp_url
        self.devices = TrustedDevices(store, sealing)
        self.limits = Limits()

    # --- clients --------------------------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.store.row("SELECT info FROM clients WHERE client_id = ?", client_id)
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull):
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or len(uris) > 10 or not all(allowed_redirect(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "Prezlab Icons accepts sign-ins from Claude only")
        self.store.run("INSERT INTO clients VALUES (?, ?, ?)", client_info.client_id,
                       client_info.model_dump_json(), time.time())

    # --- authorization ---------------------------------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Keep the checked request and send the designer to the sign-in page for it."""
        if params.resource and params.resource.rstrip("/") != self.mcp_url:
            raise AuthorizeError("invalid_target", "this sign-in is for another service")
        if any(s != SCOPE for s in params.scopes or []):
            raise AuthorizeError("invalid_scope", f"only the {SCOPE} scope is available")
        if not allowed_redirect(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "Prezlab Icons accepts sign-ins from Claude only")
        self._purge()
        request_id = secrets.token_urlsafe(32)
        self.store.run("INSERT INTO requests (id, client_id, params, expires) VALUES (?, ?, ?, ?)", _hash(request_id),
                       client.client_id, params.model_dump_json(), time.time() + REQUEST_SECONDS)
        return f"{self.issuer}/signin/{request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Code | None:
        row = self.store.row("SELECT * FROM codes WHERE code = ?", _hash(authorization_code))
        if row is None:
            return None
        if row["used"]:   # a code used twice: someone else may hold it, so its sign-in ends
            log.warning("authorization code replayed for client %s; revoking its tokens", client.client_id)
            self._revoke(row["grant_id"])
            return None
        return Code.model_validate_json(row["data"]).model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: Code
                                          ) -> OAuthToken:
        if not self.store.run("UPDATE codes SET used = 1 WHERE code = ? AND used = 0", _hash(authorization_code.code)):
            raise TokenError("invalid_grant", "authorization code already used")
        c = authorization_code
        return self._issue(c.client_id, c.grant_id, c.subject, c.scopes, c.resource,
                           int(time.time()) + SIGN_IN_DAYS * 86400)

    # --- tokens ---------------------------------------------------------------------------------------------------

    def _issue(self, client_id, grant_id, subject, scopes, resource, until: int) -> OAuthToken:
        """A new access and refresh token for a sign-in that lasts until `until`."""
        access, refresh, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        expires = min(now + ACCESS_SECONDS, until)
        for token, kind, end in ((access, "access", expires), (refresh, "refresh", until)):
            self.store.run("INSERT INTO tokens (token, kind, client_id, grant_id, subject, scopes, resource, expires) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _hash(token), kind, client_id, grant_id, subject,
                           " ".join(scopes), resource, end)
        return OAuthToken(access_token=access, expires_in=expires - now, scope=" ".join(scopes), refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Refresh | None:
        row = self.store.row("SELECT * FROM tokens WHERE token = ? AND kind = 'refresh'", _hash(refresh_token))
        if row is None:
            return None
        if row["used"] or row["revoked"]:   # an old refresh token again: stolen, or a confused client
            log.warning("refresh token reused by client %s; revoking its sign-in", row["client_id"])
            self._revoke(row["grant_id"])
            return None
        return Refresh(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                       expires_at=row["expires"], resource=row["resource"], subject=row["subject"],
                       grant_id=row["grant_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: Refresh,
                                     scopes: list[str]) -> OAuthToken:
        if not self.store.run("UPDATE tokens SET used = 1 WHERE token = ? AND used = 0 AND revoked = 0",
                              _hash(refresh_token.token)):
            self._revoke(refresh_token.grant_id)
            raise TokenError("invalid_grant", "refresh token already used")
        r = refresh_token   # the new pair lasts as long as the sign-in, not 30 days from now
        return self._issue(r.client_id, r.grant_id, r.subject, scopes, r.resource, r.expires_at)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.row("SELECT * FROM tokens WHERE token = ? AND kind = 'access' AND revoked = 0 AND expires > ?",
                             _hash(token), int(time.time()))
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=row["expires"], resource=row["resource"], subject=row["subject"],
                           claims={"iss": self.issuer})

    async def revoke_token(self, token: AccessToken | RefreshToken):
        row = self.store.row("SELECT grant_id FROM tokens WHERE token = ?", _hash(token.token))
        if row:
            self._revoke(row["grant_id"])

    def _revoke(self, grant_id: str):
        self.store.run("UPDATE tokens SET revoked = 1 WHERE grant_id = ?", grant_id)

    def _purge(self):
        now = time.time()
        self.store.run("DELETE FROM requests WHERE expires < ?", now)
        self.store.run("DELETE FROM codes WHERE expires < ?", now - 3600)
        self.store.run("DELETE FROM tokens WHERE expires < ?", int(now))
        self.store.run("DELETE FROM devices WHERE expires < ?", now)

    # --- the sign-in page ------------------------------------------------------------------------------------------

    async def signin(self, request: Request) -> Response:
        """GET: the sign-in form for a pending request. POST: the Odoo login, then the code if Odoo asks for one; on
        success, back to Claude with a one-time code."""
        key = _hash(request.path_params["request_id"])
        row = self.store.row("SELECT * FROM requests WHERE id = ? AND expires > ?", key, time.time())
        if row is None:
            return pages.render("<h1>This sign-in link has expired</h1><p>Go back to Claude and connect Prezlab Icons "
                                "again.</p>", 404)
        params = AuthorizationParams.model_validate_json(row["params"])
        asking = urlparse(str(params.redirect_uri)).hostname or "Claude"
        if request.method == "GET":
            return _code_form(row["login"]) if row["odoo_session"] else _login_form(asking)

        form = await request.form(max_files=0, max_fields=4)
        if form.get("restart"):
            self.store.run("UPDATE requests SET login = NULL, odoo_session = NULL WHERE id = ?", key)
            return _login_form(asking)
        code_step = bool(row["odoo_session"])   # Odoo has the password and waits for a 2FA code
        login = (row["login"] if code_step else str(form.get("login") or "")).strip()
        password, code = str(form.get("password") or ""), str(form.get("code") or "").strip()

        def again(message: str, status: int, at_code: bool = code_step) -> Response:
            return _code_form(login, message, status) if at_code else _login_form(asking, message, status)

        if not login or not (code if code_step else password):
            return again("Enter your code." if code_step else "Enter your Odoo login and password.", 400)
        ip = request.client.host if request.client else "?"
        if not self.limits.allow("ip", ip):
            return again("Too many sign-in attempts from this network. Try again in a few minutes.", 429)
        if not self.limits.allow("login", login.lower()):
            return again("Too many attempts for this account. Wait a few minutes before trying again.", 429)

        try:
            outcome = await odoo.login(login, password, code, row["odoo_session"] or "", self.devices)
        except Exception:
            log.exception("sign-in: unexpected failure talking to Odoo")
            return again("Sign-in isn't available right now. Try again shortly.", 502)
        if outcome.reason == "totp_required":
            self.store.run("UPDATE requests SET login = ?, odoo_session = ? WHERE id = ?", login, outcome.pending, key)
            return _code_form(login)
        if outcome.user is None:
            if outcome.detail:
                log.warning("sign-in refused (%s): %s", outcome.reason, outcome.detail)
            keep = code_step and outcome.reason == "invalid_totp"   # a wrong code can be retried; else start again
            if code_step and not keep:
                self.store.run("UPDATE requests SET login = NULL, odoo_session = NULL WHERE id = ?", key)
            status = 502 if outcome.reason in ("odoo_unreachable", "odoo_error") else 401
            return again(odoo.MESSAGES.get(outcome.reason, odoo.MESSAGES["odoo_error"]), status, keep)

        self.limits.reset("login", login.lower())
        if not self.store.run("DELETE FROM requests WHERE id = ?", key):   # the same form sent twice
            return pages.render("<h1>Already signed in</h1><p>Go back to Claude.</p>")
        code = secrets.token_urlsafe(32)
        grant = Code(code="", scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_SECONDS,
                     client_id=row["client_id"], code_challenge=params.code_challenge,
                     redirect_uri=params.redirect_uri,
                     redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                     resource=params.resource or self.mcp_url, subject=outcome.user.email, grant_id=uuid.uuid4().hex)
        self.store.run("INSERT INTO codes (code, grant_id, data, expires) VALUES (?, ?, ?, ?)", _hash(code),
                       grant.grant_id, grant.model_dump_json(), grant.expires_at)
        log.info("signed in %s for client %s", outcome.user.email, row["client_id"])
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 303,
                                headers=pages.PRIVATE)


def _login_form(asking: str, error: str = "", status: int = 200) -> Response:
    return pages.render(f"""<h1>Sign in to Prezlab Icons</h1>
<p><b>{html.escape(asking)}</b> wants to place Prezlab icons in your decks as you. Sign in with your Odoo login, the same
one you use for Image Studio.</p>{_error(error)}
<form method="post"><label for="login">Odoo login</label>
<input type="text" id="login" name="login" autocomplete="username" required autofocus>
<label for="password">Password</label>
<input type="password" id="password" name="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>
<p><small>Your password goes to Odoo only; Prezlab Icons never stores it.</small></p>""", status)


def _code_form(login: str, error: str = "", status: int = 200) -> Response:
    return pages.render(f"""<h1>Enter your code</h1>
<p>Odoo asks <b>{html.escape(login)}</b> for the 6-digit code from your authenticator app.</p>{_error(error)}
<form method="post"><label for="code">Code</label>
<input type="text" id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus>
<button type="submit">Continue</button></form>
<form method="post"><input type="hidden" name="restart" value="1"><p><small>Not you? <button type="submit"
style="background:none;color:inherit;padding:0;font-weight:400;text-decoration:underline">Start again</button></small></p>
</form>""", status)


def _error(message: str) -> str:
    return f'<p class="bad">{html.escape(message)}</p>' if message else ""


def server_settings(provider: Provider) -> dict:
    """MCPServer arguments: the SDK's OAuth endpoints on this provider, and the MCP endpoint behind its tokens (for
    this connector only, with the icons scope)."""
    return {"auth_server_provider": provider, "auth": AuthSettings(
        issuer_url=provider.issuer, resource_server_url=provider.mcp_url, required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                              default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True), validate_token_resource=True)}
