# Brand assets

`prezlab-icon.png` is the "pl:" mark in the browser tab, the same file as Image Studio's tab icon (128 px). It's served
at `/favicon.png`; when a run finishes while the tab is out of view, the page draws a red dot onto it.

`prezlab-wordmark.svg` is the Prezlab wordmark from the Prezlab Brand Kit (v1.0, 2026), as one path in `currentColor`.
The page sets its color, and only the two approved colorways are used: Off-White (`#F4F6F6`) on Dark Teal (`#002528`),
or Dark Teal on light grounds. Don't recolor, outline, stretch or respace it, and keep clear space of one colon height
on all sides. `static/index.html` and `pages.py` carry it inline, so the Claude panel needs no extra request.

The interface follows the kit the way Image Studio does: corporate palette only (Dark Teal, Slate, Light Teal, Sand,
Off-White), Figtree for all UI text (Medium at the lightest), and Orange (`#FF7C4A`) only to mark what needs attention.
