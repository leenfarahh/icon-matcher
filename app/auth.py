"""Who may call the Claude connector (connector.py). ICON_AUTH picks one of:

odoo   Each designer signs in with their Odoo login, 2FA included, as in Image Studio (oauth.py, odoo.py). The connector
       is its own OAuth server, so it needs no domain and no IT setup. The mode for the hosted connector.
token  One shared bearer token (ICON_MCP_TOKEN) in the Authorization header: for testing from Claude Code.
none   No sign-in. Only for a connector served on this machine (ICON_PUBLIC_URL on 127.0.0.1 or localhost).

Left empty, the mode is odoo when ODOO_URL is set, else token when ICON_MCP_TOKEN is set, else none."""
import hmac
import os
from urllib.parse import urlparse

from starlette.responses import JSONResponse

from . import config, oauth

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MIN_TOKEN = 32


def mode() -> str:
    """The sign-in mode from the settings. Refuses settings that would leave a reachable connector open or broken."""
    m = config.AUTH or ("odoo" if config.ODOO_URL else "token" if config.MCP_TOKEN else "none")
    if m not in ("odoo", "token", "none"):
        raise RuntimeError(f"ICON_AUTH must be odoo, token or none, not '{m}'")
    url = urlparse(config.PUBLIC_URL)
    local = url.hostname in LOCAL_HOSTS
    if os.getenv("RENDER") and local:
        raise RuntimeError("set ICON_PUBLIC_URL to this service's address, e.g. https://prezlab-icons.onrender.com")
    if m == "none" and not local:
        raise RuntimeError("a connector other machines can reach needs sign-in: set ODOO_URL and ODOO_DB (the same "
                           "as Image Studio's), or ICON_MCP_TOKEN (app/connector-setup.md)")
    if m == "odoo":
        if not (config.ODOO_URL and config.ODOO_DB):
            raise RuntimeError("Odoo sign-in needs ODOO_URL and ODOO_DB")
        if url.scheme != "https" and not local:
            raise RuntimeError("ICON_PUBLIC_URL must be an https address")
        try:
            oauth.sealing_key(config.ODOO_TD_ENCRYPTION_KEY)
        except ValueError as e:
            raise RuntimeError(str(e))
    if m == "token" and len(config.MCP_TOKEN) < MIN_TOKEN:
        raise RuntimeError(f"ICON_MCP_TOKEN needs at least {MIN_TOKEN} characters: "
                           "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return m


class RequireToken:
    """Token mode: requests to the MCP endpoint must carry "Authorization: Bearer <ICON_MCP_TOKEN>". Everything
    else (the run pages, which their own private links protect) passes through."""

    def __init__(self, app, path: str, token: str):
        self.app, self.path, self.expected = app, path.rstrip("/"), f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == self.path:
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await JSONResponse({"error": "invalid_token"}, 401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)
