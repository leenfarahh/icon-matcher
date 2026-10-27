# Icon Placer (skeleton)

Reads a .pptx, picks icons from the approved library for each item, inserts them as
brand-colored vectors, and returns a report. Mirrors the `prezlab-icon-placement` skill.

## Run

    pip install -r requirements.txt
    copy .env.example .env                 # then paste your ANTHROPIC_API_KEY into .env (optional)
    python -m app.cli deck.pptx --mode suggest
    uvicorn app.main:app --reload          # POST /jobs (deck, mode, slides), GET /jobs/{id}/download
    pytest -q                              # also validates brand_icons/icons.json; always offline

Settings (API key, model, library and work folders) live in `.env`, which `app/config.py` loads.
Real environment variables override it. Never commit or share `.env`; `.env.example` is the template.

Test UI: run `uvicorn app.main:app --reload` from this folder and open http://127.0.0.1:8000.
"Run on a deck" uploads a .pptx and shows picks, alternates and flags with icon previews;
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

Each record: `id, file, status, style, category, description, description_ar, tags, tags_ar,
preferred_for, avoid_for, contexts, metaphor_strength, pairs_with, rtl_flip, source, license`.
The concept map (concept -> house pick + alternates) is built at load time from `preferred_for`.
Field rules are in `app/library-maintenance.md`.

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

Each slide gets icons on one set: its **main component** (icon holders, diagram nodes and tiles, then short
headings, then longer text, then lists). If that can't take icons, its next set of headings or labels may;
longer text and bullets never stand in. Icons sit right next to the text itself (its real lines, alignment and
anchor, rotation included): in a node, above the label inside it; for plain headings, beside the text, clear of
every line it sits level with. An icon stays on the same surface as its text and never covers text, pictures,
lines, tables, other cards or icons (layout and master decorations included). Hero icons (section dividers,
single-message slides) are added only when the deck already has one on such a slide.

**Same structure, same treatment** (`SAME_STRUCTURE_ALL_OR_NONE`): sets with the same layout, style and place
on the slide are a family and are decided together, with one placement and size, and icons on all of them or
none. The report names the slide that holds a family back.

Concepts: Claude returns up to three ideas per item, best first, for each slide's candidate sets only (one call
per ~60 items, whole slides together, titles and sets in the prompt). A set's icons are assigned together, so
when two items' first ideas need the same icon on one slide, one falls back to its next idea. A set's icons only
count as taken once it's placed.

## Pipeline (app/pipeline.py)

| Step | Module | Status |
| --- | --- | --- |
| 1. Read deck, existing icons, settings | `deck.py` | Rows of boxes = cards, multi-paragraph box = list, lone short box = hero |
| 2. Up to three concepts per item | `concepts.py` | Claude API (candidate sets only), keyword fallback |
| 3-4. Shortlist three, pick by rules | `selector.py` | Concept map first, style, avoid_for, regional blocklist, deck ledger, all-or-none sets |
| 5. Place and insert | `placement.py`, `icon_tools.py` | SVG + PNG fallback, recolored, RTL flip only when `rtl_flip` |
| 6. Report | `report.py` | Markdown + JSON |

## Main TODOs

- Modes `replace` and `standardize` (`pipeline.py`)
- Parse `style-guide.md`; read existing icons' color and assign their sizes to tiers (`library.py`, `deck.py`)
- Real line positions for list icons, safe-margin indent, centered cards (`placement.py`)
- KPI tiles, grouped shapes, tables, inherited font sizes (`deck.py`)
- Render-and-check step (LibreOffice to PNG) before returning
- Auth, job cleanup, upload UI; Office add-in front end calling `/jobs`
