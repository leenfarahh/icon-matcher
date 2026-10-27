# Maintaining the icon library

Open this only when asked to load, add to, fix or audit the library. Picking
and inserting icons never needs it.

## Contents

1. How the library is stored
2. The library file
3. Loading the approved library (first time)
4. Adding or changing icons
5. Size tiers per brand
6. Build phases and the proof of concept

## 1. How the library is stored

The library is one flat JSON file next to the SVGs. Nothing is generated.

| File | Role | Who edits it |
|---|---|---|
| `brand_icons/icons.json` | **The library.** A flat list, one record per icon | Designers (the only file anyone edits) |
| `brand_icons/*.svg` | The icons, referenced by each record's `file` | Added by designers |

The concept map (concept -> house pick + alternates) is built when the
library loads: each concept in an icon's `preferred_for` points to that icon,
and every other icon tagged with the concept becomes an alternate.

**Rule:** after any edit, run `pytest -q`. `test_brand_library_is_valid`
checks the file (unique ids, every SVG present, valid `pairs_with`, valid
`status` / `metaphor_strength` / `rtl_flip`, no block-list hits, no concept
claimed as a house pick by two icons). Fix every failure before using the
library.

## 2. The library file

Save as **UTF-8** so Arabic survives. Lists are JSON arrays
(`["growth", "revenue"]`); `rtl_flip` is `true` / `false`.

| Field | Required | Format | Purpose |
|---|---|---|---|
| `id` | yes | lowercase-hyphen, unique | The icon's name everywhere (`increase-01`) |
| `file` | yes | file name in `brand_icons/` | The SVG |
| `status` | yes | `approved` / `sample` / `deprecated` | Only `approved` is loaded; the others are skipped |
| `style` | yes | e.g. `outline`, `filled` | One style per deck |
| `category` | yes | one word (`finance`, `people`, `risk`) | Which category file it lands in |
| `description` | yes | short English phrase | SVG title, alt text, search fallback |
| `description_ar` | no | short Arabic phrase | Arabic alt text and SVG desc |
| `tags` | yes | `;` list, English | Concepts the icon stands for |
| `tags_ar` | no | `;` list, Arabic | Same, for Arabic concepts |
| `preferred_for` | no | `;` list | Concepts where this icon is the **house pick** |
| `avoid_for` | no | `;` list | Concepts it must not be used for (`decline` on a growth arrow) |
| `contexts` | no | `;` list | Subjects where it fits best (`finance`, `governance`) |
| `metaphor_strength` | yes | `strong` / `weak` | Weak = reads as the concept only with its label |
| `pairs_with` | no | `;` list of ids | Icons that read well together in a set |
| `rtl_flip` | yes | `true` / `false` | Mirror in Arabic decks (arrows, progress, steps) |
| `source` | no | text | Where the icon came from |
| `license` | yes for approved | text | License the set is used under |

**Tagging well:** tag the ideas a slide would express, not what the icon
draws ("speed; turnaround; deadline" for a stopwatch, not "stopwatch; timer").
Give each common concept exactly one `preferred_for` icon across the library;
the test fails when two icons claim the same concept (at run time the first
record wins until it is fixed). Keep concepts specific: the offline keyword
matcher picks the longest concept found in the text, so "data protection"
beats "data". Avoid short concepts that hide inside other words ("form" in
"performance", "edit" in "credit"). `rtl_flip` matters most for
bilingual work: a directional icon pointing the wrong way is a common error
in Arabic decks.

**Regional block list:** ids or tags matching hand gestures, religious
symbols, alcohol, pig imagery or bordered maps fail the test and are
excluded by the search. The match is on substrings, so a tag like "handbook"
or "cross-functional" also trips it: reword the tag. Deprecate genuinely
blocked icons rather than retagging them to slip past the check.

## 3. Loading the approved library (first time)

The skill ships with an empty library slot: eight `sample` icons for testing
only. To load the real set:

1. **Audit** the zip or folder you received:
   `python3 scripts/audit_library.py path/to/icons.zip --out audit.md`
   It reports count, styles, stroke widths, duplicates, defects (multicolor,
   rasters, live text, gradients, fixed sizes, editor junk, unsafe content)
   and license notes. Confirm the license covers commercial use and
   modification inside client decks before going further.
2. **Normalize**:
   `python3 scripts/audit_library.py path/to/icons.zip --normalize`
   Clean copies go to `library/svg/` (color -> `currentColor`, fixed size
   removed, junk and scripts stripped, names lowercase-hyphen). Originals are
   kept in `library/originals/`. Multicolor or raster icons are held back for
   a designer.
3. **Index**: delete the sample records and sample SVGs, add a record to
   `icons.json` for each new icon (Claude can draft the tags, Arabic tags,
   categories and `rtl_flip` from the file names and a look at each icon; a
   designer must review them), then run `pytest -q`.
4. **Fix every failure**, rerun until clean.
5. **Re-package the skill folder** and upload it again. The skill reads the
   library at run time; it never writes to it.

## 4. Adding or changing icons

**Adding one icon (easiest):** in the test UI (`uvicorn app.main:app
--reload`, then http://127.0.0.1:8000), open **Library > Add icon**, choose
the SVG and fill in its fields. Saving checks the same rules as `pytest` and
writes nothing if any fails:

- **Blocked:** a duplicate or badly formed id, a concept that is already
  another icon's house pick, an unknown `pairs_with` id, a block-list term,
  and unsafe or unusable SVGs (scripts, outside links, embedded raster
  images, broken files).
- **Saved with a warning:** an empty license on an approved icon, more than
  one color, live text, gradients, and a missing viewBox.

**Not an SVG?** PNG, JPG, WebP, GIF and BMP files are traced into a
one-color SVG, so the library stays all-vector (recolorable, sharp at any
size). The form shows the original and the traced vector side by side before
you save; compare them. The original is kept in `brand_icons/originals/`.
For a clean trace, use an image of 512 px or more with the icon on a
transparent or plain background, and no tile or badge behind it. Multicolor
images are traced as one color. PDF, AI and EPS files are not accepted:
export them as SVG first (Illustrator: File > Export > Export As > SVG).

The SVG is saved as `<id>.svg` and the record is appended to `icons.json`.
House-pick concepts are added to the tags automatically, and Arabic tags lose
their diacritics so lookups match. The library reloads at once, so the new
icon can be picked straight away.

**Editing an icon (tags, house picks, descriptions, status):** in
**Library**, click **Edit** on the icon's card. The form opens pre-filled;
change what you need and save. The same checks run, and nothing is written if
one fails. The id and file can't change, because decks and other icons refer
to them. To retire an icon, set its Status to `deprecated`: it stops being
picked but stays in `icons.json`, so old decks can still be traced. Sample
and deprecated icons are hidden by default; set the **Status** filter to
**All statuses** to see and edit them.

**Adding many icons at once, or replacing an icon's image:**

1. Drop new SVGs into a folder and run the audit + normalize on it.
2. Add or edit records in `icons.json` (to retire an icon, set `status` to
   `deprecated`; don't delete the record, so old decks can still be traced).
3. `pytest -q`, fix every failure.
4. Re-package and upload the skill.

## 5. Size tiers per brand

`library/size-tiers.json` holds the default tiers in inches (`list`, `card`,
`hero`, `gap`). Add a block under `brands` once a client's tiers are agreed,
for example `"acme": {"list": 0.3, "card": 0.6, "hero": 1.2}`, and name it
with `"brand": "acme"` in the icon plan. Use an internal short key, not the
client's full name, if the skill is shared widely.

## 6. Build phases and the proof of concept

| Phase | Output |
|---|---|
| 0. Audit zip | Count, styles, defects, license check (`audit_library.py`) |
| 1. Normalize | Clean SVGs, originals kept (`--normalize`) |
| 2. Index | `icons.json` drafted and passing `pytest` |
| 3. Write skill | SKILL.md + style-guide.md (done) |
| 4. Scripts | Build, search, insert, render (done) |
| 5. POC | 50 icons, 3 decks, both environments |
| 6. Rollout | Full library, org-wide enablement |

**POC suggestion:** index 50 icons covering the concepts that recur most in
Prezlab decks (growth, cost, risk, governance, people, time, customers,
technology, data, strategy, sustainability, process). Run the skill on three
real decks (one English cards deck, one Arabic deck, one list-heavy deck) in
both environments, and record per deck: share of picks the designer kept,
alternates used, items wrongly flagged or wrongly iconed, and placement fixes
needed. Tune tags and `preferred_for` from what designers changed before
indexing the full set.
