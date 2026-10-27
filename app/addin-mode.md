# Working inside the PowerPoint add-in

Use this when the designer is working in an open deck through the PowerPoint
add-in and there is no uploaded .pptx to run the scripts on. The selection
rules (`style-guide.md`) and the report format (SKILL.md, step 6) are the
same. What changes is how you search, insert and check.

## 1. Read the slide

Work on the selected slide(s) unless the designer asked for the whole deck.
Use the add-in's tools to read each slide's shapes, text, positions and sizes,
and any pictures or icons already on it. Note:

- reading direction (Arabic text or right-to-left paragraphs),
- the theme's accent 1 color and the color of any existing icons,
- the style of existing icons (outline or filled),
- the items: cards side by side, label rows, bullet lists, as described in
  SKILL.md step 1.

## 2. Search without scripts

1. Open `brand_icons/icons.json`. Normalize the concept (lowercase,
   singular, Arabic without diacritics). The icon whose `preferred_for`
   contains it is the house pick; icons whose `tags` (or `tags_ar`) contain
   it are the candidates.
2. Skip any record with `status` other than `approved`, a `style` that is not
   the deck's, an `avoid_for` that matches the item, or an id or tag on the
   regional block list (`style-guide.md`, section 7).
3. If the concept is missing or only weak icons remain, look through the
   records in the `category` that fits the concept (tags, avoid for,
   contexts, pairs with).
4. Shortlist three, then pick with `style-guide.md`.

## 3. Prepare and insert the SVG

For each pick, read `brand_icons/<file>` and prepare the markup:

1. Replace every `currentColor` with the brand hex (for example `#4F81BD`).
2. To keep it bound to the theme, add PowerPoint's theme class to each drawn
   element and a matching style rule, as the script does:
   `<style>.MsftOfcThm_Accent1_Stroke_v2{stroke:#4F81BD;}</style>` and
   `class="MsftOfcThm_Accent1_Stroke_v2"` on each path (use `_Fill_v2` and
   `fill:` for filled icons). If the add-in can't keep the class, the plain
   hex is acceptable; say so in the report.
3. If the text reads right to left and the icon has `rtl_flip: yes`, mirror
   it: wrap the content in `<g transform="translate(W 0) scale(-1 1)">` where
   W is the viewBox width, or flip the inserted shape horizontally.
4. Insert the SVG through the add-in, then set its position and size from the
   placement rules (tier sizes from `library/size-tiers.json`; gap 0.15in).
5. Name the shape `Icon: <id> | <concept>` and set its alt text to the
   description (Arabic description in Arabic decks).

If the add-in can only insert images, not SVG, stop and tell the designer:
a PNG is not editable and does not follow the theme, so it is not an
acceptable substitute without their agreement.

## 4. Check

There is no render step in the add-in. After inserting, read back each
icon's position and size, compare it with its text (same top across a card
row, centered on the first line for lists, nothing overlapping), and ask the
designer to review the slide in place. Report exactly as in SKILL.md step 6.
