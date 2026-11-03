"""Settings. Override with environment variables or the project's .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # local settings; real environment variables win

LIBRARY_DIR = Path(os.getenv("ICON_LIBRARY_DIR", "./icons"))  # folder holding icons.json + the SVGs
WORK_DIR = Path(os.getenv("ICON_WORK_DIR", "./jobs"))
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# How much Claude thinks before it picks: low | medium | high | xhigh | max. Higher costs more and takes longer.
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "low")
# How sure Claude must be of an icon to place it: it rates each pick's fit from 1 (a stretch) to 5 (the icon nearly
# anyone would expect); 3 is a reasonable visual metaphor. Below this, the set is left for a designer to choose.
MIN_FIT = int(os.getenv("ICON_MIN_FIT", "3"))
# Prompt enhancement: Claude first reads each card (what it means, what its icon could draw) and picks with that.
# 0 turns it off: one request fewer per batch, picks from the card's text alone.
ENHANCE = os.getenv("ICON_ENHANCE", "1") != "0"
USE_LLM = bool(os.getenv("ANTHROPIC_API_KEY"))  # falls back to keyword matching without a key

# Claude connector (connector.py): the address Claude reaches it at (on Render, the service's own address unless set),
# and who may call it (auth.py).
PUBLIC_URL = (os.getenv("ICON_PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://127.0.0.1:8001").rstrip("/")
AUTH = os.getenv("ICON_AUTH", "").lower()       # secret | token | none; empty: picked from the settings below
MCP_SECRET = os.getenv("ICON_MCP_SECRET", "")   # secret mode: the secret in the connector's address (/mcp/<secret>)
MCP_TOKEN = os.getenv("ICON_MCP_TOKEN", "")     # token mode: the one shared bearer token
RUN_TTL_HOURS = float(os.getenv("ICON_RUN_TTL_HOURS", "24"))   # a run's deck, copy and links are deleted after this
MAX_DECK_MB = int(os.getenv("ICON_MAX_DECK_MB", "150"))
PARALLEL_RUNS = int(os.getenv("ICON_PARALLEL_RUNS", "2"))      # decks processed at the same time; the rest queue

# Fallback size tiers (inches on a 13.33in-wide slide). The library's style guide
# or the deck's existing icons override these.
DEFAULT_TIERS = {"list": 0.30, "card": 0.55, "column": 0.55, "hero": 1.20}
DEFAULT_STYLE = "outline"
SAFE_MARGIN_IN = 0.4
TEXT_INSET_IN = 0.1
# A slide gets icons on one set only: its main component (icon holders, diagram nodes, then headings, then
# text, then bullets; pipeline.py). If that set can't take icons, the next set of headings or labels (rank up
# to FALLBACK_MAX_RANK) may; longer text and bullets never stand in, and icons never go on two sets of a slide.
ONE_ICON_SET_PER_SLIDE = True
FALLBACK_MAX_RANK = 1
# Slides with the same structure (same layout, a component of the same style in the same part of the slide)
# are decided together: one placement and size for all, and icons on all of them or none.
SAME_STRUCTURE_ALL_OR_NONE = True

# Regional rule: never pick an icon whose id or tags hit these terms.
REGIONAL_BLOCKLIST = {
    "thumbs", "thumb-up", "ok-hand", "pointing", "handshake", "hand", "pray", "praying",
    "church", "mosque", "temple", "cross", "crescent", "star-of-david",
    "wine", "beer", "cocktail", "champagne", "toast", "alcohol",
    "pig", "piggy", "pork", "map-borders", "country-map", "region-map",
}
