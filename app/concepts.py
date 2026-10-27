"""Step 2: concepts per item (up to three, best first). Claude when a key is set, keyword fallback otherwise."""
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config
from .models import Item

SYSTEM = """You choose icon concepts for items on presentation slides. Items come grouped by slide, with the
slide's title for context, and by set: items shown together as one component, like the nodes of a diagram,
a row of cards, the headings of columns, or the bullets of one list.
For each item, return up to 3 ideas a reader could grasp from its icon at a glance, best first, each in 1-2
lowercase English words, based on meaning, not its first word ("We cut onboarding from 6 weeks to 2" ->
["speed", "efficiency", "time"]). The others are fallbacks, used when the first one's icon is already taken on
the slide, so make them genuinely different ideas a reader would still accept.
Use the slide title and the other items of the set to read each item's meaning. Return null only for an item
with no idea a pictogram could carry: names, dates, numbers, sources, taglines, or empty labels like
"Overview" or "Other". Within a set, give each item its own first idea unless two items truly mean the same.
Only the concepts in this list have icons, so use the closest ones a reader would accept for the item
(e.g. "employee engagement" -> "team"), and include at least one from the list whenever any is acceptable
(e.g. "Point of departure" -> "location" or "target"); an idea outside the list gets no icon: {vocab}
Return only JSON with one entry per item key: {{"<key>": ["<idea>", ...] or null, ...}}"""

BATCH = 60          # items per request (whole slides kept together): each JSON reply stays far below MAX_TOKENS
MAX_TOKENS = 4096
WORKERS = 4         # batches sent at the same time


def extract(items: list[Item], vocab: list[str], priority: list[str] = ()) -> list[str]:
    """Sets item.concept in place. `priority` = concept-map keys, preferred over plain tags.
    Returns notes for the report, e.g. when the API failed and keyword matching stood in."""
    if not items:
        return []
    if config.USE_LLM:
        return _extract_llm(items, vocab, priority)
    _extract_keywords(items, vocab, priority)
    return []


def _key(i: Item) -> str:
    return f"{i.slide}:{i.shape_id}:{i.paragraph}"


def _extract_llm(items, vocab, priority=(), client=None) -> list[str]:
    import anthropic
    client = client or anthropic.Anthropic()
    terms = list(dict.fromkeys([*priority, *vocab]))  # house-pick concepts first, then the other tags
    system = [{"type": "text", "text": SYSTEM.format(vocab=", ".join(terms)),
               "cache_control": {"type": "ephemeral"}}]  # same for every batch: cached after the first

    def run(batch):
        try:
            data = _label(client, system, batch)
        except (anthropic.APIError, ValueError) as e:
            return (batch, *_failure(e))
        for i in batch:
            got = data.get(_key(i))
            got = [got] if isinstance(got, str) else (got if isinstance(got, list) else [])
            i.concepts = list(dict.fromkeys(c.strip().lower() for c in got if isinstance(c, str) and c.strip()))[:3]
            i.concept = i.concepts[0] if i.concepts else None
        return None

    batches = _batches(items)
    results = [run(batches[0])]  # alone first: it writes the cache, and a bad key or model stops here
    if results[0] and results[0][2]:
        results = [(items, results[0][1], True)]
    elif len(batches) > 1:
        with ThreadPoolExecutor(WORKERS) as pool:
            results += list(pool.map(run, batches[1:]))

    failed = [r for r in results if r]
    if not failed:
        return []
    stand_in = [i for batch, _, _ in failed for i in batch]
    _extract_keywords(stand_in, vocab, priority)
    reasons = "; ".join(dict.fromkeys(reason for _, reason, _ in failed))
    return [f"Claude couldn't label {len(stand_in)} of {len(items)} items ({reasons}); "
            "keyword matching was used for them, so expect rougher picks there"]


def _batches(items) -> list[list]:
    """Up to BATCH items per request, whole slides together, each slide's sets kept in order."""
    slides = defaultdict(list)
    for i in items:
        slides[i.slide].append(i)
    batches, current = [], []
    for slide_items in slides.values():
        first, keyed = {}, []
        for k, i in enumerate(slide_items):   # each set together, where it first appears
            group = first.setdefault(i.set_id or f"solo-{k}", k)
            keyed.append((group, k, i))
        slide_items = [i for _, _, i in sorted(keyed, key=lambda x: x[:2])]
        if current and len(current) + len(slide_items) > BATCH:
            batches.append(current)
            current = []
        current += slide_items
    return batches + ([current] if current else [])


def _describe(batch) -> str:
    """The batch as the model sees it: slides with their titles, then each set and its items."""
    lines, slide, group = [], None, object()
    for i in batch:
        if i.slide != slide:
            slide, group = i.slide, object()
            lines.append(f"Slide {i.slide}" + (f": {i.slide_title[:160]}" if i.slide_title else ""))
        if i.set_id is None or i.set_id != group:
            group = i.set_id
            size = sum(1 for j in batch if j.set_id == i.set_id) if i.set_id else 1
            kind = "a bulleted list" if i.kind == "list" else ("a single message" if i.set_id is None else "a set")
            lines.append(f"  {kind} of {size} item{'s' if size != 1 else ''}:")
        lines.append(f"    {_key(i)} | {' '.join(i.text.split())[:300]}")
    return "\n".join(lines)


def _label(client, system, batch) -> dict:
    """One request for a batch. A reply cut off at MAX_TOKENS is retried as two half batches."""
    msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, system=system,
                                 messages=[{"role": "user", "content": _describe(batch)}])
    if msg.stop_reason == "max_tokens" and len(batch) > 1:
        half = len(batch) // 2
        return {**_label(client, system, batch[:half]), **_label(client, system, batch[half:])}
    text = "".join(b.text for b in msg.content if b.type == "text")  # skips thinking blocks on newer models
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON in the reply, stop reason {msg.stop_reason}")
    return json.loads(text[start:end + 1])


def _failure(e) -> tuple[str, bool]:
    """A readable reason, and whether it will fail for every batch (so the rest aren't sent)."""
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "the API key was rejected: check ANTHROPIC_API_KEY in .env", True
    if isinstance(e, anthropic.PermissionDeniedError):
        return "the API key isn't allowed to use this model", True
    if isinstance(e, anthropic.NotFoundError):
        return f"model '{config.ANTHROPIC_MODEL}' not found: check ANTHROPIC_MODEL in .env", True
    if isinstance(e, anthropic.BadRequestError):
        return f"the API rejected the request ({e.message})", True
    if isinstance(e, anthropic.RateLimitError):
        return "rate limited by the API", False
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}", False
    if isinstance(e, anthropic.APIConnectionError):
        return "couldn't reach the Claude API", False
    return f"unreadable reply: {e}", False


def _extract_keywords(items, vocab, priority=()):
    """Offline fallback for tests: the longest concept-map term found in the text, then the longest tag."""
    order = sorted(priority, key=len, reverse=True) + sorted(set(vocab) - set(priority), key=len, reverse=True)
    for i in items:
        low = i.text.lower()
        i.concepts = [v for v in order if v in low][:3]
        i.concept = i.concepts[0] if i.concepts else None
