"""CLI: python -m app.cli deck.pptx --library ./brand_icons --mode suggest"""
import argparse
from pathlib import Path

from . import config, pipeline, report
from .library import Library

ap = argparse.ArgumentParser()
ap.add_argument("deck", type=Path)
ap.add_argument("--library", type=Path, default=config.LIBRARY_DIR)
ap.add_argument("--mode", default="add")
ap.add_argument("--slides", default="", help="e.g. 2,3,5")
a = ap.parse_args()
r = pipeline.run(a.deck, Library(a.library), a.mode, [int(s) for s in a.slides.split(",") if s] or None)
print(report.to_markdown(r))
if r.output_file:
    print(f"\nSaved: {r.output_file}")
