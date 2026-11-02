# Claude connector: setup

The connector (`app/connector.py`) lets designers run the icon placer from Claude chat, Cowork, Claude Code and the
Claude add-in for PowerPoint. Claude gives the designer a private upload link, the deck runs on the server, and the
designer gets a new copy with the icons; the original file is never changed. Flagged placeholders are settled in the
chat: Claude offers each one's closest icons and adds the designer's picks to a fresh copy.

Who does what:

| Step | Who | Where |
| --- | --- | --- |
| 1. Pick the address (e.g. `https://icons.prezlab.com`) | Tool owner + whoever manages prezlab.com DNS | DNS |
| 2. Register the app | Entra ID admin | Microsoft Entra admin center |
| 3. Deploy | Tool owner | Render |
| 4. Add the connector | Claude organization Owner | claude.ai |
| 5. Connect and use | Each designer | Claude, PowerPoint add-in |

## 1. The address

The connector needs its own HTTPS address on a prezlab.com subdomain, such as `https://icons.prezlab.com`. Render's
default `*.onrender.com` address won't do: Claude asks Entra ID for a token for the MCP URL itself, so that URL must be
the app's Application ID URI, and Entra accepts an HTTPS Application ID URI only on a domain verified in the tenant.

Below, `https://icons.prezlab.com` stands for the address you choose. The MCP URL is that plus `/mcp`.

## 2. Entra ID app registration

In the Microsoft Entra admin center, under App registrations:

1. **New registration.** Name: `Prezlab Icons connector`. Supported account types: this organizational directory
   only. Redirect URI: platform **Web**, `https://claude.ai/api/mcp/auth_callback`.
2. **Manifest.** Set `requestedAccessTokenVersion` (under `api`) to `2`. Both token versions work; version 2 is
   the one tested.
3. **Expose an API.** Set the Application ID URI to the MCP URL, exactly: `https://icons.prezlab.com/mcp`. Add a
   scope named `access_as_user` (consent: admins and users; display name "Use Prezlab Icons").
4. **API permissions.** Add a permission, My APIs, `Prezlab Icons connector`, `access_as_user`. Then **Grant admin
   consent**, so designers aren't asked when they connect.
5. **Certificates & secrets.** New client secret. Copy its value for step 4 (the Claude connector), not for the
   server. Note when it expires: the connector stops signing people in on that date until a new secret is entered.
6. From **Overview**, note the **Directory (tenant) ID** and **Application (client) ID**.

If sign-in fails with `AADSTS9010010`, the Application ID URI doesn't match the MCP URL exactly.

## 3. Deploy on Render

`render.yaml` describes the service: Python 3.12, one paid instance (Starter) with a 5 GB disk for runs, Frankfurt
region. Free instances have no disk and sleep when idle, which loses runs in progress.

1. Push this repository to GitHub.
2. In Render: **New**, **Blueprint**, pick the repository. Render reads `render.yaml` and asks for:

   | Setting | Value |
   | --- | --- |
   | `ICON_PUBLIC_URL` | `https://icons.prezlab.com` (no trailing slash) |
   | `ENTRA_TENANT_ID` | Directory (tenant) ID from step 2 |
   | `ENTRA_CLIENT_ID` | Application (client) ID from step 2 |
   | `ANTHROPIC_API_KEY` | The key Claude uses to pick icons; without it, keyword matching (rougher picks) |

3. In the service's **Settings**, **Custom Domains**: add `icons.prezlab.com`, then add the CNAME record Render shows
   at prezlab.com's DNS. Render issues the certificate once the record resolves.
4. Check `https://icons.prezlab.com/health` (shows the icon count) and
   `https://icons.prezlab.com/.well-known/oauth-protected-resource/mcp` (names your tenant).

Optional settings (`.env.example` lists them all): `ICON_RUN_TTL_HOURS` (24), `ICON_MAX_DECK_MB` (150),
`ICON_PARALLEL_RUNS` (2), `ANTHROPIC_MODEL` (`claude-sonnet-5-5`), `ANTHROPIC_EFFORT` (`low`).

The icon library ships with the code (`icons/`). To publish library changes, commit them and Render redeploys.

## 4. Add the connector in Claude

An Owner, in claude.ai: **Organization settings**, **Connectors**, **Add custom connector**
([Anthropic's guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).

- Name: `Prezlab Icons`
- URL: `https://icons.prezlab.com/mcp`
- Advanced settings: OAuth Client ID (the Application ID from step 2) and Client Secret (from step 2.5)

## 5. Designers

1. **Connect once.** In Claude, open Connectors, choose Prezlab Icons, **Connect**, and sign in with the Microsoft
   365 account.
2. **PowerPoint add-in.** Click **+** below the chat input, **Connectors**, and turn on Prezlab Icons. Optional:
   in the add-in's Settings, Instructions, add "For icons, use the Prezlab Icons connector", so Claude doesn't
   reach for other icon sources.
3. **Ask.** For example: "Replace the PL placeholders in this deck with Prezlab icons", "Add Prezlab icons to
   slides 3 to 6", or "Suggest icons for this deck, don't add them yet". Claude gives an upload link. In
   PowerPoint, save the deck first, then upload the saved file.
4. **Download.** The upload page shows progress and then the download. Claude reports what went in and what was
   flagged, and offers the closest icons for each flagged placeholder; your picks go into a fresh copy at the
   same link.

## Try it locally

    uvicorn app.connector:app --port 8001
    claude mcp add --transport http prezlab-icons http://127.0.0.1:8001/mcp

On 127.0.0.1 the connector runs without sign-in. For a shared test server without Entra ID, set `ICON_MCP_TOKEN`
(32+ characters) and add `--header "Authorization: Bearer <token>"` to `claude mcp add`. claude.ai can take a token
only where the organization has request-header connectors (a beta), so the hosted connector uses Entra ID.

## Security and data

- **Sign-in.** `/mcp` accepts only Entra ID tokens from Prezlab's tenant, issued for this app, carrying
  `access_as_user`. A run belongs to the designer who started it: no one else's Claude can read or change it.
- **Run links.** The upload page and download sit behind a private link, unique to the run. Whoever holds the link
  can upload once and download the copy until the run expires, so it shouldn't be pasted anywhere shared.
- **Retention.** The uploaded deck, its copy and the link are deleted after `ICON_RUN_TTL_HOURS` (24 hours by
  default; checked hourly). They sit on the Render disk in Frankfurt until then. Confirm that region with whoever
  owns Prezlab's data policy before client decks go through it.
- **Claude API.** With `ANTHROPIC_API_KEY` set, the slide text that needs icons goes to the Claude API to pick them,
  as it does in the local tool.
- **Logs.** Access logs are off, because run links carry their token in the path.
- **Test UI.** `app.main` (the local test UI) has no sign-in and is never deployed: keep it on 127.0.0.1.

## Limits

- The connector returns a new copy; it doesn't edit the open deck in place.
- One instance: runs live on its disk and are processed there, two at a time.
- Flagged placeholders are chosen in the chat, by what each icon draws. Slide pictures need PowerPoint on the server,
  so the hosted connector has none (the local test UI does).
