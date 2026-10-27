# Icon selection and placement rules

Read before picking icons on every run. Each rule says what to do and why, so
you can judge the cases the rule doesn't name.

## Contents

1. The rules at a glance
2. Choosing the icon
3. Restraint: when to flag instead
4. Style, weight, color
5. Size and placement
6. Consistency across the deck
7. Regional rules
8. Arabic and right-to-left decks

## 1. The rules at a glance

| Rule | Standard |
|---|---|
| Style | One style per deck; match existing icons if present |
| Weight | Stroke weight close to body text weight |
| Size | Fixed tiers (card icon, list icon, hero icon) set per brand |
| Color | Single brand color or text color; no multicolor |
| Restraint | Skip icons where no clear metaphor exists; flag instead of forcing |
| Consistency | Same concept = same icon, deck-wide |
| Regional | Avoid hand gestures, religious symbols, alcohol, pig imagery, and maps showing borders |

## 2. Choosing the icon

An icon earns its place when a reader scanning the slide gets the item's idea
from the icon before reading the words. Pick for that.

**Order of preference among the shortlist:**

1. The **house pick** for the concept (`preferred_for` in `icons.json`). The
   library team chose it for this concept; use it unless a rule below rules it
   out or the item's context changes the meaning.
2. A **strong** metaphor over a weak one. `metaphor_strength: weak` means the
   icon reads as the concept only with the label beside it (a light bulb for
   "innovation", an arrow for "next steps"). Weak icons are fine on rows and
   lists where the label sits right next to them; avoid them as the only
   anchor on a hero slide.
3. **Context fit**: an icon whose `contexts` match the slide's subject.
4. **Pairs well with the set**: in a row of cards, prefer icons listed in each
   other's `pairs_with`, so the set reads as one family.

**Never pick an icon whose `avoid_for` matches the item.** "Growth" arrows on
a "cost reduction" card read as costs going up. The search already removes
these when the words match; watch for the cases where the meaning matches but
the words don't ("lower spend" vs "cost reduction").

**Literal beats clever.** "Customers" gets people, not a heart. "Compliance"
gets a shield or checklist, not a gavel, unless the slide is about courts.

**The concept is the idea, not the noun.** "Reduce onboarding time from 10 to
3 days" is about speed (clock, stopwatch), not onboarding (person + plus).
"Board approval of the policy" is about governance or approval, not a board
table.

## 3. Restraint: when to flag instead

Flag the item, place nothing, and say why in the report when:

- The item has no concrete idea: taglines, values ("Our heritage",
  "Excellence"), "Overview", "Introduction", "Other".
- The best shortlist entry is `low` confidence, or weak with nothing better in
  the category file.
- The only fitting icons are on the regional block list.
- One item of a set has no honest icon: flag the **whole set** (cards,
  columns, a list) rather than leave one card blank or force a poor icon. A
  half-iconed set looks unfinished. If the designer asks for icons on the
  others anyway, place them and note the gap.
- There is no room: no free space above a card heading, a list over seven
  bullets, a table cell, a chart. Do not shrink or move content to make room
  beyond the small indent a list or row needs.

A clear "no icon, here's why" is a better outcome than a forced metaphor the
designer has to find and remove.

## 4. Style, weight, color

**Style.** If the deck already has icons, use their style (the inspection
reports `deck_icon_style`). If they are mixed, use the majority and report the
others as inconsistent. With no icons, use the library's main style. Never mix
outline and filled in one deck: they read as two design hands.

**Weight.** Outline icons should look about as heavy as the body text beside
them. The library is normalized to one stroke weight per style, so the weight
you control is size: an outline icon at list tier next to bold 20pt text can
look thin. If the render shows icons visibly lighter or heavier than the
text, report it for the designer rather than switching style.

**Color.** One color for all icons in the deck:

1. The color of the deck's existing icons, if they agree.
2. Otherwise the theme's accent 1 (`theme:accent1`).
3. A designer override (any theme slot, or a hex) wins over both.

Prefer theme slots over hex: the inserted SVG is bound to the theme slot, so
if the brand color changes the icons follow. Use the text color
(`theme:dk1` / `theme:dk2`) when accent 1 has poor contrast on the card fill
(below 3:1) or the brand reserves the accent for highlights. Never multicolor.

## 5. Size and placement

**Tiers** (inches, long side, from `library/size-tiers.json`; a brand block
overrides the defaults):

| Tier | Default | Used for |
|---|---|---|
| list | 0.32 | Bullets, label rows, agenda lines |
| card | 0.55 | Cards, columns, pillars, process steps |
| hero | 1.1 | Section dividers, single-message slides |
| gap | 0.15 | Space between an icon and its text |

Use one tier per set. Tiers are sizes, not item types: for lists and rows the
icon should be roughly the height of the first line of text, so a bullet list
in 32pt text takes the card tier (the inspection suggests the closer tier,
and the script warns when an icon is far off its line). If neither tier fits,
ask for a brand tier rather than using a one-off size inside a set.

**Placement:**

- **Cards and columns (`above`)**: the icon sits above the heading, one gap
  above the first line, aligned with the heading's start (left in English,
  right in Arabic, centered if the heading is centered). Every card in the
  row gets the icon at the same height.
- **Rows (`leading`)**: the icon sits on the reading-start side of the label,
  centered on the label when it is one or two lines, on its first line when
  longer. If the margin has no room, the label's box is narrowed by the icon
  width plus gap and the icon takes that space (reported as a layout change).
- **Bullet lists (`leading` + paragraph)**: the icon replaces the bullet,
  centered on the bullet's first line. The whole list is indented by icon plus
  gap so the text column stays aligned, including any bullet you skip.
- **Hero (`above`)**: only on section dividers or single-message slides, and
  only when the designer asks for hero icons or the deck already uses them.
- **Replace**: the new icon takes the old one's footprint and center.

## 6. Consistency across the deck

- Same concept = same icon on every slide. "Security" on slide 3 and slide 11
  gets the same shield.
- One icon never means two things on the same slide.
- Across the deck, avoid reusing one icon for different concepts where the
  library offers a distinct fitting icon; readers learn icon meanings as they
  go.
- Icons in a set share one tier, one placement, one alignment.
- When adding to a deck that already has our icons (`library_id` in the
  inspection), reuse their concept-to-icon choices.

## 7. Regional rules

Prezlab works across the UAE, KSA, Jordan and Qatar for government, banking
and consulting clients. Never place:

- hand gestures (thumbs up, OK sign, pointing finger, fist, waving hand),
- religious symbols of any faith (crosses, crescents, places of worship),
- alcohol (wine, beer, cocktails, champagne toasts),
- pigs or pig imagery (including piggy banks: use a coin stack or wallet),
- maps that show national borders (use a globe or a region-neutral pin).

The search excludes icons whose id or tags hit the block list, but tags can
be incomplete: look at each pick's description too. "Partnership" handshake
icons are borderline (two hands, not a gesture) and fine unless the client's
brand guide says otherwise.

## 8. Arabic and right-to-left decks

- **Mirror directional icons.** Arrows, "next", progress, timelines, steps and
  forward motion point in the reading direction. Icons with `rtl_flip: yes`
  are mirrored automatically when their text box reads right to left. If a
  pick is clearly directional but not marked `rtl_flip`, set `"mirror": true`
  on that placement and tell the designer to fix `icons.json` (and the reverse,
  `"mirror": false`, for an icon wrongly marked).
- **Never mirror** clocks, checkmarks, charts with a fixed axis, text-bearing
  icons, logos, or anything a reader recognizes as an object with a real
  orientation.
- Icons sit on the **right** of rows and bullets and **right-aligned** above
  card headings, matching the text's start.
- Alt text uses the Arabic description when the library has one.
- Write the report in Arabic when the designer wrote in Arabic (Modern
  Standard Arabic).
