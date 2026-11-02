# Claude connector: setup

The connector (`app/connector.py`) lets designers run the icon placer from Claude chat, Cowork, Claude Code and the
Claude add-in for PowerPoint. In Claude web, desktop and mobile it opens a **Prezlab Icons panel** in the conversation:
the same page as the local test console, for one run. The designer uploads the deck, reviews the picks, chooses icons
for flagged placeholders on their slides and downloads the new copy, all inside Claude; the original file is never
changed. Where Claude shows no panel, Claude gives a private link that opens the same page in the browser.

Designers sign in with their **Odoo login**, exactly as in Image Studio, 2FA included. The connector runs its own
sign-in, so it lives on Render's free `onrender.com` address: no domain, no DNS record, nothing for IT to set up.

| Step | Who | Where |
| --- | --- | --- |
| 1. Deploy | Tool owner | Render |
| 2. Add the connector | Claude organization Owner | claude.ai |
| 3. Connect and use | Each designer | Claude, PowerPoint add-in |

## 1. Deploy on Render

`render.yaml` describes the service: Docker (the `Dockerfile`: Python 3.12 plus LibreOffice, which makes the slide
pictures for the review screen), one Standard instance (2 GB) with a 5 GB disk, Frankfurt region. The disk keeps
sign-ins and runs across restarts and redeploys. A Free instance works for a trial, but it has no disk and sleeps after
15 idle minutes: everyone then signs in again, and runs in progress are lost. On Starter (512 MB), slide pictures of
large decks can run out of memory.

1. Push this repository to GitHub.
2. In Render: **New**, **Blueprint**, pick the repository. Render reads `render.yaml` and asks for these. The Odoo
   values are the same as Image Studio's, so copy them from its Environment tab:

   | Setting | Value |
   | --- | --- |
   | `ODOO_URL` | Image Studio's `ODOO_URL` (e.g. `https://prezlab.odoo.com`) |
   | `ODOO_DB` | Image Studio's `ODOO_DB` |
   | `ODOO_TD_ENCRYPTION_KEY` | A new key: `python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` |
   | `ANTHROPIC_API_KEY` | The key Claude uses to pick icons; without it, keyword matching (rougher picks) |

   Creating the service by hand (**New**, **Web Service**) instead works too: runtime **Docker**, no build or start
   command (the `Dockerfile` has them), a disk mounted at `/var/data`, and the `envVars` from `render.yaml` with the
   values above.
3. Once it's live, open `https://<service>.onrender.com/health`: it shows the icon count.

The service's address comes from Render (`RENDER_EXTERNAL_URL`). Set `ICON_PUBLIC_URL` only to use another address.

**A service created on the Python runtime** (before slide pictures) moves to Docker in place, keeping its address,
settings, disk and sign-ins: **Settings**, **Build & Deploy**, **Source**, **Edit**, Runtime **Docker**, Dockerfile
path `./Dockerfile`, Docker command empty, **Save**; then **Manual Deploy**. The first build takes 5 to 10 minutes
(it installs LibreOffice); later ones reuse it.

Optional settings (`.env.example` lists them all): `ICON_RUN_TTL_HOURS` (24), `ICON_MAX_DECK_MB` (150),
`ICON_PARALLEL_RUNS` (2), `ANTHROPIC_MODEL` (`claude-sonnet-5-5`), `ANTHROPIC_EFFORT` (`low`),
`ICON_MIN_FIT` (3: how sure Claude must be to place an icon), `ICON_ENHANCE` (1: read each card before picking),
`ODOO_ALLOWED_EMAIL_DOMAIN` (`prezlab.com`).

The icon library ships with the code (`icons/`). To publish library changes, commit them and Render redeploys.

## 2. Add the connector in Claude

An Owner, in claude.ai: **Organization settings**, **Connectors**, **Add custom connector**
([Anthropic's guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).

- Name: `Prezlab Icons`
- URL: `https://<service>.onrender.com/mcp`
- Advanced settings: leave empty. Claude registers itself.

## 3. Designers

1. **Connect once.** In Claude, open Connectors, choose Prezlab Icons, **Connect**. A Prezlab Icons page opens: sign
   in with your Odoo login (and your 2FA code, if Odoo asks). A sign-in lasts 30 days.
2. **PowerPoint add-in.** Click **+** below the chat input, **Connectors**, and turn on Prezlab Icons. Optional:
   in the add-in's Settings, Instructions, add "For icons, use the Prezlab Icons connector", so Claude doesn't
   reach for other icon sources.
3. **Ask.** For example: "Replace the PL placeholders in this deck with Prezlab icons", "Add Prezlab icons to
   slides 3 to 6", or "Suggest icons for this deck, don't add them yet". The Prezlab Icons panel opens in the chat.
   In PowerPoint, save the deck first, then upload the saved file.
4. **Work in the panel**, as in the local tool: choose the deck, **Run**, review the picks, **Choose flagged icons**
   (click a highlighted placeholder, then one of its closest icons, a library search result or your own upload),
   **Add chosen**, **Download deck**. Claude saves the download for you. **Full screen** gives the panel room;
   **Open in browser** opens the same page in a browser tab. Claude hears what you did in the panel, so you can also
   ask it about the result or have it choose icons in the chat.
5. **No panel?** Where Claude shows none (Claude Code, possibly the PowerPoint add-in), Claude gives a link instead:
   it opens the same page in the browser.

## Try it locally

    uvicorn app.connector:app --port 8001
    claude mcp add --transport http prezlab-icons http://127.0.0.1:8001/mcp

On 127.0.0.1 the connector runs without sign-in unless `ODOO_URL` is set. To test a deployed copy from Claude Code
without Odoo, set `ICON_MCP_TOKEN` (32+ characters) and add `--header "Authorization: Bearer <token>"` to
`claude mcp add`.

## Security and data

- **Sign-in.** The connector is its own OAuth server (`app/oauth.py`) and checks logins against Odoo
  (`app/odoo.py`, ported from Image Studio). Only active, internal Odoo users with an `@prezlab.com` email get in;
  portal accounts are refused. Attempts are limited per network and per login. Sign-ins come back only to Claude's own
  addresses (claude.ai, claude.com, or this machine for Claude Code).
- **Passwords.** Passed to Odoo and never stored or logged. For 2FA users, Odoo's trusted-device key is kept, sealed
  with `ODOO_TD_ENCRYPTION_KEY`, so the code is asked for once every 30 days. Changing the key only means designers
  enter a code once more.
- **Tokens.** Stored as hashes on the service's disk (`ICON_AUTH_DB`). Access tokens last an hour and are for this
  connector only; refresh tokens rotate, and a reused one ends that sign-in. To sign everyone out, delete
  `/var/data/runs/auth.sqlite3` and restart the service. Archiving someone in Odoo stops new sign-ins at once; an
  existing sign-in runs out within 30 days.
- **Runs.** A run belongs to the designer who started it: no one else's Claude can read or change it. The page and
  everything it reaches (the deck upload, the results, the download) sit behind a private link, unique to the run, so
  it shouldn't be pasted anywhere shared. The panel calls that link straight from the designer's browser; the
  connector answers only Claude's panel origin (`*.claudemcpcontent.com`) and its own pages, and the panel may reach
  no other server (its declared CSP).
- **Library.** The panel's Library tab reads the library; adding and editing icons stays in the local tool, since
  changes on Render would be lost at the next deploy.
- **Retention.** The uploaded deck, its copy and the link are deleted after `ICON_RUN_TTL_HOURS` (24 hours by
  default; checked hourly). They sit on the Render disk in Frankfurt until then. Confirm that region with whoever
  owns Prezlab's data policy before client decks go through it.
- **Claude API.** With `ANTHROPIC_API_KEY` set, the slide text that needs icons goes to the Claude API to pick them,
  as it does in the local tool.
- **Logs.** Access logs are off, because run links and sign-in pages carry secrets in the path.
- **Test UI.** `app.main` (the local test UI) has no sign-in and is never deployed: keep it on 127.0.0.1.

## Limits

- The connector returns a new copy; it doesn't edit the open deck in place.
- One instance: runs and sign-ins live on its disk, and decks are processed there, two at a time.
- The Odoo 2FA step drives Odoo's login pages, as Image Studio does, so an Odoo upgrade that changes them can break
  it for 2FA users until `app/odoo.py` is updated; the sign-in page then says so.
- Slide pictures need PowerPoint on the server, so on Render the review shows the flagged placeholders on a blank
  slide (the local test UI shows the real slide).
- An inline panel has a height limit; long results fit better in **Full screen**.
- The panel's client is vendored (`app/static/vendor/`, MCP Apps 1.7.5); updating it is a file copy (see its README).
