# Icon Placer (skeleton)

Reads a .pptx, replaces the PL logo placeholders designers left for icons with icons from the approved
library (or finds spots for icons itself), inserts them as brand-colored vectors, and returns a report.
Mirrors the `prezlab-icon-placement` skill.

## Run

    pip install -r requirements.txt
    copy .env.example .env                 # then paste your ANTHROPIC_API_KEY into .env (optional)
    python -m app.cli deck.pptx            # --mode replace (default), suggest or add
    uvicorn app.main:app --reload          # POST /jobs (deck, mode, slides), GET /jobs/{id}/download
    uvicorn app.connector:app --port 8001  # the Claude connector (MCP at /mcp); see below
    pytest -q                              # also validates brand_icons/icons.json; always offline

Modes: **replace** (default) swaps each PL logo placeholder for an icon and adds nothing anywhere else.
**add** does that too, and on slides without placeholders finds where icons fit (see Items and placement).
**suggest** plans what add would do without touching the deck; the UI then adds the chosen picks.

## Claude connector (app/connector.py)

A remote MCP server, so designers can run the tool from Claude chat, Cowork, Claude Code and the Claude add-in for
PowerPoint. `start_icon_run` opens the **Prezlab Icons panel** in the conversation (an MCP App): this test console
(`app/static/index.html`) for one run, against that run's private endpoints, with the bridge to Claude in
`app/static/panel.js`. Where Claude shows no panel, the run's private link opens the same page in the browser. Decks
never pass through the chat: a deck attached to the message is sent by Claude from its code tools straight to the run
(`start_icon_run`'s `attached_deck`), so the panel has it ready. `get_icon_run` reports the picks and flags,
`choose_icons` writes a fresh copy from the chat, and `search_icons` looks up the library. Designers don't sign in:
the hosted connector's address carries a secret (`/mcp/<ICON_MCP_SECRET>`, `app/auth.py`), so it runs on Render's own
address with no domain. A run's deck, copy and link are deleted after 24 hours. Saved runs and the apply step are shared with the
test UI (`app/jobs.py`). Hosting on Render (`render.yaml`) and adding the connector in
Claude: `app/connector-setup.md`.

## PL logo placeholders (app/placeholders.py)

Designers drop the PL logo (as vector shapes, copied from the template) where an icon should go. The tool
recognizes every copy by its look: the same outlines and parts in the same arrangement as a reference copy,
at any position, size or color. **Setup:** paste one placeholder, exactly as designers use it, onto a blank
slide and save it as a .pptx in `icons/placeholders/` (every shape on its slides is one reference; add more
files for other variants). Until then, only shapes named or alt-texted "PL", "PL logo", "Prezlab logo" or
"Icon placeholder" count.

Each placeholder stands for its label: the heading of the panel it sits on, else the nearest text in line with
it. Pairs go closest first and a text labels one placeholder only; KPI values ("15-30%", "AED 2.4bn") are never
labels, so a tile's icon stands for its description. A slide's placeholders are one set (all of them get icons,
or none). The icon takes the placeholder's place: same group and z-order, centered in its frame at the largest
size that keeps the icon's proportions; the placeholder is removed. Its color is the placeholder's theme color
when that has 3:1 contrast with what is behind it (the logo itself left out), else the usual deck color rules.
Placeholders left in place (no label, or no icon fits) are named in the report with the reason.

Settings (API key, model, library and work folders) live in `.env`, which `app/config.py` loads.
Real environment variables override it. Never commit or share `.env`; `.env.example` is the template.

Test UI: run `uvicorn app.main:app --reload` from this folder and open http://127.0.0.1:8000.
"Run on a deck" uploads a .pptx and shows picks, alternates and flags with icon previews. PL placeholders
left without an icon appear on pictures of their slides, highlighted ("Choose flagged icons" next to Download
goes there): the designer clicks one, chooses from its closest icons, searches the library or uploads an icon
of their own (SVG, or PNG/JPG traced; checked like a library upload, kept with the run, not added to the
library), sees it in place, and "Add chosen" writes a new copy (`GET /jobs/{id}/slides/{n}.png`,
`POST /jobs/{id}/uploads`, `POST /jobs/{id}/apply` with `choices`). Slide pictures are made by
PowerPoint (`app/render.py`), so they need PowerPoint on the machine running the tool; without it the
placeholders show on a blank slide;
"Library" browses `icons.json` by tag (English or Arabic) and shows the house pick for a concept;
"Add icon" uploads an SVG with its fields, checks it against the same rules as `pytest`, and appends it
to `icons.json` (`POST /library/icons`). PNG, JPG, WebP, GIF and BMP uploads are traced into a one-color
SVG with vtracer (MIT, pip only) and previewed before saving; the original goes to `brand_icons/originals/`.
"Edit" on a card changes an icon's fields with the same checks (`PATCH /library/icons/{id}`; fields left out
keep their value; the id and file never change). Set Status to deprecated to retire an icon. The UI has no login: keep the server on 127.0.0.1.

The PNG fallback inside each inserted icon is rendered with resvg (`resvg-py`), a self-contained pip
package: no system libraries and no admin rights needed.

## Library layout

    brand_icons/
      icons.json                one flat list, one record per icon (the only file anyone edits)
      <Name>.svg                the icons, referenced by each record's `file`

Each record: `id, file, status, style, family, category, description, description_ar, tags, tags_ar,
preferred_for, avoid_for, contexts, metaphor_strength, pairs_with, rtl_flip, source, license`.
The concept map (concept -> house pick + alternates) is built at load time from `preferred_for`; lookups
allow for plurals and UK spelling ("organisations"). Claude doesn't use it (it picks from its own knowledge);
it drives keyword matching when there is no API key. Field rules are in `app/library-maintenance.md`.

**Icon families** (`family`): the library holds two visual families, `simple` (bold, minimal glyphs: the house
set) and `detailed` (finer line icons with more elements). They read as two design hands, so a slide's set of
icons never mixes them.

## Icon colors (app/colors.py)

Always a theme slot (accent1-6, tx1/bg1, tx2/bg2), resolved through each slide's own master, and bound in
the SVG with PowerPoint's `MsftOfcThm_*` classes so icons follow the theme. The deck color is the theme slot
the deck's existing icons use when at least two thirds agree (read from SVG theme classes, SVG paint, PNG
pixels or native shapes), else accent1. Per icon set, against what is behind the icon (slide background,
layout/master decorations and every filled shape under it, transparency blended): the deck color at 3:1 or
more, else the label's own text color at 2:1 or more, else the theme's dark/light color at 3:1. The
report's Color column says which and why. Icons keep their proportions when inserted.

## Items and placement (app/deck.py, app/placement.py)

Titles, footers, page/step numbers and source notes are never items. Sets are **components**: elements that
look alike, however they're arranged (all nodes of a diagram, all headings of a set of columns, the bullet
lists under each column). Text drawn in its own shape, or sitting on a node-sized filled shape, takes that
shape's outline, fill and size; plain text takes its font and how it lines up. A box is a list only when its
paragraphs carry bullets or numbering. A set gets icons on every member or none.

Text inside groups counts. **Empty icon holders** (two or more alike circles, rings or rounded squares
with nothing in them) are found and paired with their label (the text on the panel they sit in, else the
nearest text in line); the icon goes centered in the holder, at 60% of its inner circle.

Each slide gets icons on one set: its **main component** (PL placeholders, which then take the slide on their
own, then icon holders, diagram nodes and tiles, then short
headings, then longer text, then lists). If that can't take icons, its next set of headings or labels may;
longer text and bullets never stand in. Icons sit right next to the text itself (its real lines, alignment and
anchor, rotation included): in a node, above the label inside it; for plain headings, beside the text, clear of
every line it sits level with. An icon stays on the same surface as its text and never covers text, pictures,
lines, tables, other cards or icons (layout and master decorations included). Hero icons (section dividers,
single-message slides) are added only when the deck already has one on such a slide.

**Same structure, same treatment** (`SAME_STRUCTURE_ALL_OR_NONE`): sets with the same layout, style and place
on the slide are a family and are decided together, with one placement and size, and icons on all of them or
none. The report names the slide that holds a family back.

Icons: Claude picks **one icon per item** by meaning, from its own knowledge of visual metaphors (`SYSTEM` in
`app/concepts.py`). It gets the icon library (each icon's name, family, what it draws, keywords and "not for"
terms; no house picks) and the slides' candidate sets, each set as one slide entry of numbered points (a
headline and a gist), and returns `{"choices":[{"slide", "point", "icon"}]}`. Its rules: exact library names,
"" when nothing genuinely fits (but a reasonable metaphor beats ""), no icon twice on a slide, one family per
slide, all or nothing per slide. One call per ~60 items, whole slides together; the library is cached after the
first call.

The code checks every set's choices against the rules it can verify (names in the library, no repeats, one
family, all or nothing). Sets that break one go back to Claude **once**, with their earlier choices and the rule
broken; a set that still breaks one is flagged with the reason. The selector then applies the hard rules (style,
`avoid_for`, regional block list, an icon once per slide, all or none per set) and **same label, same icon**
deck-wide: a recurring heading keeps the icon it got first, where its set still holds. A set's icons only count
as taken once it's placed.

Without a key, or when the API fails, keyword matching finds up to three ideas per item and the concept map and
tags find their icons, with the same rules.

## Pipeline (app/pipeline.py)

| Step | Module | Status |
| --- | --- | --- |
| 1. Read deck, existing icons, settings | `deck.py` | Rows of boxes = cards, multi-paragraph box = list, lone short box = hero |
| 2. One icon per item | `concepts.py` | Claude picks from the icon library by meaning (candidate sets only), one retry for sets that break a rule; keyword fallback |
| 3-4. Check by rules | `selector.py` | Style, avoid_for, regional blocklist, an icon once per slide, one family per set, same label same icon, all-or-none sets; concept map and tags for keyword matching |
| 5. Place and insert | `placement.py`, `icon_tools.py` | SVG + PNG fallback, recolored, RTL flip only when `rtl_flip` |
| 6. Report | `report.py` | Markdown + JSON |

## Main TODOs

- Mode `standardize` (`pipeline.py`)
- Parse `style-guide.md`; read existing icons' color and assign their sizes to tiers (`library.py`, `deck.py`)
- Real line positions for list icons, safe-margin indent, centered cards (`placement.py`)
- KPI tiles, grouped shapes, tables, inherited font sizes (`deck.py`)
- Render-and-check step (LibreOffice to PNG) before returning
- Connector: take OneDrive/SharePoint links instead of an upload (Microsoft Graph); a visual picker for flagged
  placeholders in Claude (MCP App)
