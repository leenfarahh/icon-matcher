# Claude connector: setup

The connector (`app/connector.py`) lets designers run the icon placer from Claude chat, Cowork, Claude Code and the
Claude add-in for PowerPoint. In Claude web, desktop and mobile it opens a **Prezlab Icons panel** in the conversation:
the same page as the local test console, for one run. A deck the designer attached to their message is already in the
panel; they review the picks, choose icons for flagged placeholders on their slides and download the new copy, all
inside Claude. The original file is never changed. Where Claude shows no panel, Claude gives a private link that opens
the same page in the browser.

**Designers don't sign in.** Instead the connector's address carries a secret (`/mcp/<ICON_MCP_SECRET>`): the
Owner adds that address in claude.ai once, and only clients with it reach the tools. It runs on Render's free
`onrender.com` address: no domain, no DNS record, nothing for IT to set up.

| Step | Who | Where |
| --- | --- | --- |
| 1. Deploy | Tool owner | Render |
| 2. Add the connector, allow attached decks | Claude organization Owner | claude.ai |
| 3. Connect and use | Each designer | Claude, PowerPoint add-in |

## 1. Deploy on Render

`render.yaml` describes the service: Docker (the `Dockerfile`: Python 3.12 plus LibreOffice, which makes the slide
pictures for the review screen), one Standard instance (2 GB) with a 5 GB disk, Frankfurt region. The disk keeps runs
across restarts and redeploys. A Free instance works for a trial, but it has no disk and sleeps after 15 idle minutes:
runs in progress are lost. On Starter (512 MB), slide pictures of large decks can run out of memory.

1. Push this repository to GitHub.
2. In Render: **New**, **Blueprint**, pick the repository. Render reads `render.yaml` and asks for these:

   | Setting | Value |
   | --- | --- |
   | `ICON_MCP_SECRET` | A new secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `ANTHROPIC_API_KEY` | The key Claude uses to pick icons; without it, keyword matching (rougher picks) |

   Creating the service by hand (**New**, **Web Service**) instead works too: runtime **Docker**, no build or start
   command (the `Dockerfile` has them), a disk mounted at `/var/data`, and the `envVars` from `render.yaml` with the
   values above.
3. Once it's live, open `https://<service>.onrender.com/health`: it shows the icon count.

The service refuses to start without `ICON_MCP_SECRET` (it would be open to anyone); Render then keeps the previous
version running. Its address comes from Render (`RENDER_EXTERNAL_URL`). Set `ICON_PUBLIC_URL` only to use another one.

**Moving a service that used Odoo sign-in for the connector:** in Render, **Environment**, add `ICON_MCP_SECRET` and
delete `ODOO_TD_ENCRYPTION_KEY`; keep `ODOO_URL`, `ODOO_DB` and `ODOO_ALLOWED_EMAIL_DOMAIN` for the web page (see
"Web page for people without Claude"), or delete them too to keep it off. Save and deploy. The old sign-ins file
(`/var/data/runs/auth.sqlite3`) is no longer read and can be deleted from the service's **Shell**. Then do step 2 with
the new address.

**A service created on the Python runtime** (before slide pictures; it shows a Build Command and a Start Command)
has no LibreOffice, and Render can't switch it to Docker: its settings offer no runtime. Create a new service from
the Blueprint as above, then add the connector again in claude.ai with its address, have the Owner allow its domain
for code execution (step 2), and delete the old service (runs last a day, so nothing is lost). The first build takes
5 to 10 minutes (it installs LibreOffice); later ones reuse it.

Optional settings (`.env.example` lists them all): `ICON_RUN_IDLE_HOURS` (1),
`ICON_PARALLEL_RUNS` (2), `ANTHROPIC_MODEL` (`claude-sonnet-5-5`), `ANTHROPIC_EFFORT` (`low`),
`ICON_MIN_FIT` (3: how sure Claude must be to place an icon), `ICON_ENHANCE` (1: read each card before picking).

The icon library ships with the code (`icons/`). To publish library changes, commit them and Render redeploys.

## 2. In claude.ai (Organization Owner)

**Add the connector.** **Organization settings**, **Connectors**, **Add custom connector**
([Anthropic's guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).

- Name: `Prezlab Icons`
- URL: `https://<service>.onrender.com/mcp/<ICON_MCP_SECRET>`
- Advanced settings: leave empty.

Replacing an Odoo sign-in connector: edit its URL to the one above, or remove it and add it again if Claude still asks
designers to sign in.

**After a deploy that changes the tools or the panel, remove the connector and add it again.** Claude keeps each
connector's tool list from when it was added, so changes otherwise don't reach it. The connector's page in Claude
should then count one interactive tool (Open Prezlab Icons); if it counts none, Claude won't show the panel.

**Let attached decks through.** Claude sends a deck attached to the message from its code execution sandbox, which
reaches only the domains the organization allows. **Organization settings**, **Capabilities**, code execution's
network access: allow specific domains and add `<service>.onrender.com`. Without it, the panel still opens, Claude
says the deck didn't come through, and designers choose it in the panel instead.

## 3. Designers

1. **Connect once.** In Claude, open Connectors, choose Prezlab Icons, **Connect**. There's no sign-in.
2. **PowerPoint add-in.** Click **+** below the chat input, **Connectors**, and turn on Prezlab Icons. Optional:
   in the add-in's Settings, Instructions, add "For icons, use the Prezlab Icons connector", so Claude doesn't
   reach for other icon sources.
3. **Attach the deck and ask.** For example: "Replace the PL placeholders in this deck with Prezlab icons", "Add
   Prezlab icons to slides 3 to 6", or "Suggest icons for this deck, don't add them yet". The Prezlab Icons panel
   opens in the chat, and the deck appears in it a few seconds later, once Claude has sent it. Without an attachment
   (or if it doesn't come through), choose the deck in the panel. In PowerPoint, save the deck first, then upload
   the saved file.
4. **Work in the panel**, as in the local tool: check the mode, **Run**, review the picks, **Choose flagged icons**
   (click a highlighted placeholder, then one of its closest icons, a library search result or your own upload),
   **Add chosen**, **Download deck**. Claude saves the download for you. **Full screen** gives the panel room;
   **Open in browser** opens the same page in a browser tab. Claude hears what you did in the panel, so you can also
   ask it about the result or have it choose icons in the chat.
5. **No panel?** Where Claude shows none (Claude Code, possibly the PowerPoint add-in), Claude gives a link instead:
   it opens the same page in the browser.

## Web page for people without Claude

The same service also serves Prezlab Icons as a plain web page, for colleagues who don't have Claude. They sign in
with their Odoo login, as in Image Studio. It is the same page as Claude's panel, so it works the same way: upload
the deck, **Run**, review, choose flagged icons, download. It needs no new service, only the Odoo settings. The Claude
connector is unchanged: no sign-in, reached through its secret address.

**Turn it on (tool owner).** In Render, open the existing service, **Environment**, and add the same values as Image
Studio's service:

| Setting | Value |
| --- | --- |
| `ODOO_URL` | e.g. `https://prezlab.odoo.com`, no trailing slash |
| `ODOO_DB` | The Odoo database name |
| `ODOO_ALLOWED_EMAIL_DOMAIN` | `prezlab.com` (the default), or empty to allow any |

Save; Render redeploys. The page is then at `https://<service>.onrender.com` (which leads to `/web`); share that
address with the team. The service refuses to start if only one of `ODOO_URL` and `ODOO_DB` is set or `ODOO_URL`
isn't https; Render then keeps the previous version running. A service that still has these from the old Odoo
sign-in turns the page on at its next deploy. `ODOO_TD_ENCRYPTION_KEY` isn't used.

**Use it (each person).**

1. Open the address and sign in with your Odoo login and password. With two-factor on, enter the code from your
   authenticator app; that browser isn't asked for one again for 30 days. You stay signed in for 30 days.
2. Click **Start with a deck** (right after signing in, a run opens by itself). A private page opens for that deck,
   the same one Claude's panel shows, and you can run the deck again there after changes. The address bar keeps
   showing the web page, so a refresh comes back to it. Leaving or refreshing deletes the deck and its copy, so
   download the copy first.
3. In PowerPoint, save the deck first, then upload it, check the mode, **Run**, review, **Choose flagged icons**,
   **Add chosen**, **Download deck**.

**Access.** Only active internal Odoo users with an email on `ODOO_ALLOWED_EMAIL_DOMAIN` get in; portal accounts
(clients) and archived accounts are refused. Opening the address (or a Teams or Outlook link preview) never starts a
run; only the button does. Sign-in attempts are limited to 20 per address and 6 per login every 5 minutes. Someone
archived in Odoo can't sign in again, but a browser already signed in keeps working until its 30 days are up; to sign
everyone out at once, delete `/var/data/runs/web.key` from the service's **Shell**. To turn the page off, delete
`ODOO_URL` and `ODOO_DB`.

## Usage page (tool owners)

The service records how the tool is used and shows it at `https://<service>.onrender.com/web/usage`: runs per day,
where they come from (each Claude app, or the web page), how many were downloaded, how many PL placeholders were filled
automatically, icons placed, failures, the most used icons, searches in Claude that found nothing (candidates for new
icons), web page users, the Claude apps that connect (and whether each can show the panel), and Claude API tokens. It
shows the last 7, 30, 90 or 365 days, and **Download CSV** exports that period's events for Excel.

**Turn it on.** It needs the web page (above). In Render, **Environment**, add `ICON_USAGE_ADMINS`: the Odoo emails
of the people who may see it, comma-separated. Save; Render redeploys. They open the address and sign in with Odoo;
signing in there leads to the usage page and starts no run. Anyone else signed in is turned away. The page's dates
are in Jordan time (UTC+3, all year). Optional: `ICON_USAGE_UTC_OFFSET` (hours from UTC, e.g. `4` for the UAE),
`ICON_USAGE=0` to stop recording.

**What it keeps.** One small SQLite file, `/var/data/runs/usage.db`, beside the runs on the service's disk, so it
outlives runs and deploys. Per event: the time, the run's random id, Claude (and which Claude app) or the web page (and
the email of who signed in there), the mode, counts (icons placed, placeholders found and filled, flags, icons chosen
by hand, tokens), how long the deck took, library icon ids, and the words of a Claude search that found nothing (first
60 characters). Never a deck's name, its text or labels. Claude runs aren't tied to a person, since the connector has
no sign-in. The Claude app is the name each app gives when it connects; where two apps connect with the same user
agent, runs show that user agent instead. To start over, delete `usage.db` from the service's **Shell**.

## Try it locally

    uvicorn app.connector:app --port 8001
    claude mcp add --transport http prezlab-icons http://127.0.0.1:8001/mcp

On 127.0.0.1 the connector is open at `/mcp` unless `ICON_MCP_SECRET` is set. To use the deployed connector from
Claude Code: `claude mcp add --transport http prezlab-icons https://<service>.onrender.com/mcp/<ICON_MCP_SECRET>`.
Claude Code sends a deck you name by its path on your machine, so it needs no allowed domain.

## Security and data

- **Access.** Designers don't sign in. The tools answer only at `/mcp/<ICON_MCP_SECRET>`; every other address gets
  404. Anyone who has that address can run decks through the connector, on Prezlab's Claude API key, so treat it as a
  password: keep it in claude.ai's connector settings and Render, never in chats, documents or tickets. If it leaks,
  set a new `ICON_MCP_SECRET` in Render and update the connector's URL in claude.ai.
- **Runs.** Without sign-in a run isn't tied to a designer; it is reached through its id and its private link, which
  only the conversation (or the web page's browser) that started it has. The page and everything it reaches (the deck upload, the results, the
  download) sit behind that link, so it shouldn't be pasted anywhere shared. The panel calls that link straight from
  the designer's browser; the connector answers only Claude's panel origin (`*.claudemcpcontent.com`) and its own
  pages, and the panel may reach no other server (its declared CSP).
- **Web page.** At `/web`, behind Odoo sign-in (`app/odoo.py`, ported from Image Studio). The password goes to Odoo
  only and is never stored or logged. There is no sign-in database: the browser keeps HTTP-only cookies for `/web`,
  sealed with AES-256-GCM under a key only the server holds (`web.key` on the disk), for who signed in, a two-factor
  step in progress (15 minutes) and the trusted-device key Odoo gives a browser after a code (it skips the code only,
  never the password). Runs started there are the same private runs as Claude's, with the same retention.
- **Attached decks.** Claude sends the attachment from its code execution sandbox straight to the run's private link
  over HTTPS. The deck's contents never go through the chat.
- **Library.** The panel's Library tab reads the library; adding and editing icons stays in the local tool, since
  changes on Render would be lost at the next deploy.
- **Retention.** No deck content is kept in a database. A run's deck, its copy, its slide pictures and the link live
  on the Render disk in Frankfurt only while the run is in use: a run from the web page is deleted as soon as the
  designer leaves or refreshes its page, and any run (Claude's too) after `ICON_RUN_IDLE_HOURS` unused (1 hour by
  default, checked every 5 minutes; an open page keeps its run in use). Decks have no size cap. Confirm that region
  with whoever owns Prezlab's data policy before client decks go through it.
- **Usage.** `usage.db` on the same disk keeps counts, icon ids and web page users' emails, never deck content, with
  no expiry (see "Usage page"). Only the people in `ICON_USAGE_ADMINS` can see it.
- **Claude API.** With `ANTHROPIC_API_KEY` set, the slide text that needs icons goes to the Claude API to pick them,
  as it does in the local tool.
- **Logs.** Access logs are off, because the connector's address and the run links carry secrets in the path. The
  web page logs each sign-in and each run it starts with the person's email.
- **Test UI.** `app.main` (the local test UI) has no protection at all and is never deployed: keep it on 127.0.0.1.

## Limits

- The connector returns a new copy; it doesn't edit the open deck in place.
- One instance: runs live on its disk, and decks are processed there, two at a time.
- Attached decks need Claude's code execution with network access to the connector (step 2). Where Claude has no
  code execution, designers choose the deck in the panel.
- Slide pictures on Render come from LibreOffice, whose fonts can differ a little from PowerPoint's. Without it (a
  Python-runtime service) the review shows the flagged placeholders on a blank slide and says why under it.
- An inline panel has a height limit; long results fit better in **Full screen**.
- The panel's client is vendored (`app/static/vendor/`, MCP Apps 1.7.5); updating it is a file copy (see its README).
