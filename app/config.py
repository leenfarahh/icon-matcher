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
USE_LLM = bool(os.getenv("ANTHROPIC_API_KEY"))  # falls back to keyword matching without a key

# Claude connector (connector.py): the address Claude reaches it at, and who may call it (auth.py).
PUBLIC_URL = os.getenv("ICON_PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")
AUTH = os.getenv("ICON_AUTH", "").lower()       # entra | token | none; empty: picked from the settings below
MCP_TOKEN = os.getenv("ICON_MCP_TOKEN", "")     # token mode: the one shared bearer token
ENTRA_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "")
ENTRA_SCOPE = os.getenv("ENTRA_SCOPE", "access_as_user")
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
