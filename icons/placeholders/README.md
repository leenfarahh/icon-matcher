# PL logo placeholder references

The confirmed placeholder is `pl-logo.svg` (the white pl: mark on a dark teal circle). Designers insert that
file, or copy the "PL logo" picture from `pl-logo.pptx`, wherever an icon should go. `pl-logo.pptx` is its
reference, taken from a real deck (PowerPoint rewrites an SVG's path data on insert). An SVG placeholder is
recognized by the shapes it draws, however its path data is written, so the original file, PowerPoint's
rewrite, a recolored copy or one saved with different metadata all count. If designers convert it to shapes (Ungroup / Convert to Shape), paste one
converted copy into a second .pptx here, since that is a different look.

Put a .pptx here holding the PL logo placeholder exactly as designers paste it into decks (copy it from the
template or a real deck onto a blank slide, then save). Every shape on its slides is one reference look; add
another file for each variant (for example a light and a dark version, or a ring-and-letters version).

The tool then recognizes every copy of that placeholder in a deck, however it is moved, resized or recolored,
and replaces it with an icon. Run `pytest -q` after adding a file: the library must still load cleanly.
