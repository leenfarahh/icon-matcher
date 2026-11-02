"""Who may call the Claude connector (connector.py). ICON_AUTH picks one of:

entra  Each designer signs in with their Microsoft 365 account (OAuth through Microsoft Entra ID). Claude finds the
       sign-in page through this server's protected resource metadata, and every token is checked against the
       tenant's signing keys. The mode for a hosted connector; setup in connector-setup.md.
token  One shared bearer token (ICON_MCP_TOKEN) in the Authorization header: for Claude Code, and for Claude
       organizations that can add a connector with request headers (a beta).
none   No sign-in. Only for a connector served on this machine (ICON_PUBLIC_URL on 127.0.0.1 or localhost).

Left empty, the mode is entra when ENTRA_TENANT_ID is set, else token when ICON_MCP_TOKEN is set, else none."""
import hmac
from urllib.parse import urlparse

import anyio
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from starlette.responses import JSONResponse

from . import config

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MIN_TOKEN = 32


def mode() -> str:
    """The sign-in mode from the settings. Refuses settings that would leave a reachable connector open."""
    m = config.AUTH or ("entra" if config.ENTRA_TENANT_ID else "token" if config.MCP_TOKEN else "none")
    if m not in ("entra", "token", "none"):
        raise RuntimeError(f"ICON_AUTH must be entra, token or none, not '{m}'")
    if m == "none" and urlparse(config.PUBLIC_URL).hostname not in LOCAL_HOSTS:
        raise RuntimeError("a connector other machines can reach needs sign-in: set ENTRA_TENANT_ID and "
                           "ENTRA_CLIENT_ID, or ICON_MCP_TOKEN (app/connector-setup.md)")
    if m == "entra" and not (config.ENTRA_TENANT_ID and config.ENTRA_CLIENT_ID):
        raise RuntimeError("Entra sign-in needs ENTRA_TENANT_ID and ENTRA_CLIENT_ID")
    if m == "token" and len(config.MCP_TOKEN) < MIN_TOKEN:
        raise RuntimeError(f"ICON_MCP_TOKEN needs at least {MIN_TOKEN} characters: "
                           "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return m


def entra(mcp_url: str) -> dict:
    """MCPServer arguments for Entra sign-in: 401s point Claude at the protected resource metadata, which names the
    tenant as the authorization server and the scope to ask for (its full name: the MCP URL is the app's
    Application ID URI)."""
    tenant = config.ENTRA_TENANT_ID
    settings = AuthSettings(issuer_url=f"https://login.microsoftonline.com/{tenant}/v2.0",
                            resource_server_url=mcp_url, required_scopes=[f"{mcp_url}/{config.ENTRA_SCOPE}"],
                            validate_token_resource=False)   # EntraTokenVerifier checks the audience itself
    return {"auth": settings, "token_verifier": EntraTokenVerifier(tenant, config.ENTRA_CLIENT_ID, mcp_url)}


class EntraTokenVerifier:
    """Accepts a Microsoft Entra ID access token only when it is signed by the tenant's keys, issued by the tenant,
    meant for this app (its client id, or its Application ID URI: the MCP URL) and not expired. Its scopes come back
    by their full names, so they compare with the required scope; its subject is the user's object id (oid)."""

    def __init__(self, tenant: str, client_id: str, app_id_uri: str, jwks_client=None):
        self.tenant, self.app_id_uri = tenant, app_id_uri
        self.audiences = [client_id, app_id_uri]
        self.issuers = [f"https://login.microsoftonline.com/{tenant}/v2.0", f"https://sts.windows.net/{tenant}/"]
        self.jwks = jwks_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", cache_keys=True, lifespan=3600)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await anyio.to_thread.run_sync(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audiences, issuer=self.issuers,
                                options={"require": ["exp", "iss", "aud"]}, leeway=60)
        except (jwt.PyJWTError, OSError):   # a bad or expired token, or Entra's keys out of reach
            return None
        if claims.get("tid") != self.tenant:
            return None
        return AccessToken(token=token, client_id=claims.get("azp") or claims.get("appid") or "",
                           scopes=[f"{self.app_id_uri}/{s}" for s in claims.get("scp", "").split()],
                           expires_at=claims["exp"], subject=claims.get("oid") or claims.get("sub"),
                           claims={"iss": claims["iss"]})


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
