"""Who may call the Claude connector (connector.py). Designers never sign in. ICON_AUTH picks one of:

secret  The connector's address carries a secret: /mcp/<ICON_MCP_SECRET>, and /mcp itself leads nowhere. The Owner
        adds that address in claude.ai once; whoever doesn't have it can't reach the tools. The mode for the hosted
        connector.
token   One shared bearer token (ICON_MCP_TOKEN) in the Authorization header: for testing from Claude Code.
none    Open at /mcp. Only for a connector served on this machine (ICON_PUBLIC_URL on 127.0.0.1 or localhost).

Left empty, the mode is secret when ICON_MCP_SECRET is set, else token when ICON_MCP_TOKEN is set, else none."""
import hmac
import os
import re
from urllib.parse import urlparse

from starlette.responses import JSONResponse

from . import config

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MIN_TOKEN = 32
URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")   # the secret sits in the address


def mode() -> str:
    """The mode from the settings. Refuses settings that would leave a reachable connector open or broken."""
    m = config.AUTH or ("secret" if config.MCP_SECRET else "token" if config.MCP_TOKEN else "none")
    if m not in ("secret", "token", "none"):
        raise RuntimeError(f"ICON_AUTH must be secret, token or none, not '{m}'")
    url = urlparse(config.PUBLIC_URL)
    local = url.hostname in LOCAL_HOSTS
    if os.getenv("RENDER") and local:
        raise RuntimeError("set ICON_PUBLIC_URL to this service's address, e.g. https://prezlab-icons.onrender.com")
    if m == "none" and not local:
        raise RuntimeError("a connector other machines can reach must not be open: set ICON_MCP_SECRET, and its "
                           "address becomes /mcp/<secret> (app/connector-setup.md)")
    if m == "secret":
        if len(config.MCP_SECRET) < MIN_TOKEN or not URL_SAFE.fullmatch(config.MCP_SECRET):
            raise RuntimeError(f"ICON_MCP_SECRET needs at least {MIN_TOKEN} letters, digits, - or _: "
                               "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        if url.scheme != "https" and not local:
            raise RuntimeError("ICON_PUBLIC_URL must be an https address: the secret travels in it")
    if m == "token" and len(config.MCP_TOKEN) < MIN_TOKEN:
        raise RuntimeError(f"ICON_MCP_TOKEN needs at least {MIN_TOKEN} characters: "
                           "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return m


def mcp_path(m: str, base: str = "/mcp") -> str:
    """Where the MCP endpoint is served in this mode."""
    return f"{base}/{config.MCP_SECRET}" if m == "secret" else base


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
