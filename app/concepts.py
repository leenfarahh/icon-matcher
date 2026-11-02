"""Step 2: which icon each item gets. With a key, Claude picks ONE library icon per item by meaning, from its own
knowledge of visual metaphors (it gets the icon library, not the concept map). Each candidate set goes to it as one
slide entry, and it follows the rules in SYSTEM: exact library names, no icon twice on a slide, one style family
per slide, all or nothing. Without a key, or when the API fails, keyword matching finds up to three ideas and the
selector looks their icons up in the concept map and tags. For PL placeholders still left without an icon, a second
request (CLOSEST) ranks the nearest icons for a designer to choose from."""
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config
from .models import Item

SYSTEM = """You choose icons for presentation slides. You receive an ICON LIBRARY and a set of SLIDES. Each slide
has numbered points (a headline and a short gist). For every point, pick the ONE library icon that best
represents what the point means.

ICON LIBRARY format: one icon per line: its name, then " | " and what is known about it ({fields}). Always return
the name only: the text before the first " | ".

SLIDES format: each slide is one group of points shown together, like a row of cards, the nodes of a diagram or
the headings of columns, with its deck slide number and title for context. A deck slide with more than one such
group appears as more than one slide here; treat each as a slide of its own.

How to choose, in this order
1. Library by meaning. Use your own knowledge of visual metaphors: choose the icon a reader would connect with
   the point at a glance, judged by what it draws, not by a shared word. "Cyber resilience" is a shield or a
   lock. "Always-on coordination" can be a headset, phone, clock or people icon. "Extension option" can be
   arrows, refresh or settings. "Manual processes" are work done by hand, not a user manual.
2. Nothing fits. If no icon gives a genuine match, return "". A wrong icon is worse than no icon. Do not be
   timid either: a reasonable visual metaphor beats "".

Rules
3. Every icon you return must appear in the ICON LIBRARY, copied exactly as written. Never invent, shorten,
   translate or combine names.
4. Do not use the same icon twice on one slide.
5. Keep each slide consistent: {style_rule}
6. All or nothing per slide: if any point on a slide gets "", return "" for every point on that slide. A
   half-matched row of icons looks worse than the original icons.
7. Use the slide and point numbers exactly as given (they start at 0). Return one entry for every point of every
   slide.
8. Return JSON only, in the output format below. No commentary.

Output format:
{{"choices":[{{"slide":<slide number>,"point":<point number>,"icon":"<library name or empty>"}}]}}

ICON LIBRARY
{library}"""

STYLE_RULE = ("when the library has style variants of the same pictogram (outline, filled, dual tone), use one "
              "style for every point on that slide.")
FAMILY_RULE = ("the library comes in style families, named right after each icon's name ({names}). Use one "
               "family for every point on that slide: first pick the family that has a genuine icon for every "
               "point, then choose all of that slide's icons from it.")
RETRY = ("Your earlier choices for these slides broke the rules noted under each one. Choose again for every point "
         "of these slides, following all the rules.")
FAMILY_LOOKS = {"simple": "bold, minimal glyphs", "detailed": "finer line drawings with more elements"}

CLOSEST = """A different task from the one above. The points in SLIDES got no icon, and a designer will choose one for
each by hand from a short list. For every point, list up to {n} library icons closest to what it means, best first.
They need not be perfect matches: an icon a reader could reasonably connect with the point beats none, so list at
least one for every point. Rules 1, 3 and 7 still apply; rules 2, 4, 5 and 6 and the output format above do not.
Return JSON only, in this format:
{{"options":[{{"slide":<slide number>,"point":<point number>,"icons":["<library name>", ...]}}]}}"""

BATCH = 60          # items per request (whole slides kept together): each JSON reply stays far below MAX_TOKENS
MAX_TOKENS = 4096
WORKERS = 4         # batches sent at the same time
OPTIONS = 4         # closest icons offered for a flagged placeholder
CLOSEST_BATCH = 15  # items per CLOSEST request: longer replies, so smaller batches sent side by side


def extract(items: list[Item], library, style: str) -> list[str]:
    """Sets item.icon_pick (with Claude), item.concept and item.concepts in place.
    Returns notes for the report, e.g. when the API failed and keyword matching stood in."""
    if not items:
        return []
    if config.USE_LLM:
        return _extract_llm(items, library, style)
    _extract_keywords(items, library)
    return []


def _key(i: Item) -> str:
    return f"{i.slide}:{i.shape_id}:{i.paragraph}"


def _pickable(library, style: str) -> list:
    """The icons Claude may pick: approved, in the deck's style, clear of the regional block list."""
    return [i for i in library.icons.values() if i.style == style and not any(
        b in h.lower() for b in config.REGIONAL_BLOCKLIST for h in [i.id, *i.tags])]


def system_prompt(library, style: str) -> str:
    """The rules and the icon library: names, families, drawings and keywords. No house picks (concept map)."""
    icons = _pickable(library, style)
    names = sorted({i.family for i in icons if i.family})
    lines = []
    for icon in icons:
        about = [icon.family, icon.description, ", ".join(t.lower() for t in icon.tags)]
        if icon.avoid_for:
            about.append(f"not for: {', '.join(icon.avoid_for)}")
        lines.append(" | ".join([icon.id, *(a for a in about if a)]))
    if names:
        looks = "; ".join(f'"{n}" = {FAMILY_LOOKS[n]}' if n in FAMILY_LOOKS else f'"{n}"' for n in names)
        fields, style_rule = "its style family, what it draws, keywords", FAMILY_RULE.format(names=looks)
    else:
        fields, style_rule = "what it draws, keywords", STYLE_RULE
    return SYSTEM.format(fields=fields, style_rule=style_rule, library="\n".join(lines))


def _extract_llm(items, library, style, client=None) -> list[str]:
    import anthropic
    client = client or anthropic.Anthropic()
    system = [{"type": "text", "text": system_prompt(library, style),
               "cache_control": {"type": "ephemeral"}}]  # same for every batch: cached after the first
    known = {i.id: i.family for i in _pickable(library, style)}   # what Claude may name -> its family
    invented = []

    def run(batch):
        try:
            data = _label(client, system, batch)
        except (anthropic.APIError, ValueError) as e:
            return (batch, *_failure(e))
        notes = {}
        for g in _groups(batch):   # sets whose choices break a rule go back once, with what they broke
            got = [data.get(_key(i), "") for i in g]
            broken = _broken(got, known)
            if broken:
                earlier = ", ".join(f'{p} = "{pick}"' for p, pick in enumerate(got))
                notes[_key(g[0])] = f"Earlier choices: {earlier}. Broke {broken}."
        if notes:
            try:
                data.update(_label(client, system, [i for g in _groups(batch) if _key(g[0]) in notes for i in g],
                                   notes))
            except (anthropic.APIError, ValueError):
                pass   # keep the first answer: the selector flags the sets that still break a rule
        for i in batch:
            pick = data.get(_key(i), "")
            if pick and pick not in known:   # rule 3: a name not in the library counts as no icon
                invented.append(pick)
                pick = ""
            i.icon_pick, i.concept, i.concepts = pick, pick or None, [pick] if pick else []
        return None

    batches = _batches(items)
    results = [run(batches[0])]  # alone first: it writes the cache, and a bad key or model stops here
    if results[0] and results[0][2]:
        results = [(items, results[0][1], True)]
    elif len(batches) > 1:
        with ThreadPoolExecutor(WORKERS) as pool:
            results += list(pool.map(run, batches[1:]))

    notes = []
    if invented:
        notes.append(f"Claude named {len(invented)} icons that aren't in the library "
                     f"({', '.join(sorted(set(invented))[:5])}); those items got no icon")
    failed = [r for r in results if r]
    if not failed:
        return notes
    stand_in = [i for batch, _, _ in failed for i in batch]
    _extract_keywords(stand_in, library)
    reasons = "; ".join(dict.fromkeys(reason for _, reason, _ in failed))
    return notes + [f"Claude couldn't label {len(stand_in)} of {len(items)} items ({reasons}); "
                    "keyword matching was used for them, so expect rougher picks there"]


def _batches(items, size: int = BATCH) -> list[list]:
    """Up to `size` items per request, whole slides together, each slide's sets kept in order."""
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
        if current and len(current) + len(slide_items) > size:
            batches.append(current)
            current = []
        current += slide_items
    return batches + ([current] if current else [])


def _groups(batch) -> list[list[Item]]:
    """The batch's sets in order (a lone item is a set of one): each is one SLIDE entry for the model."""
    where, groups = {}, []
    for i in batch:
        k = (i.slide, i.set_id or f"solo-{id(i)}")
        if k not in where:
            where[k] = len(groups)
            groups.append([])
        groups[where[k]].append(i)
    return groups


def _broken(got: list[str], known: dict) -> str:
    """The rules a set's choices break, of those code can check (3 to 6); "" when it keeps them all."""
    named = [g for g in got if g]
    problems = [f'rule 3 ("{g}" is not in the ICON LIBRARY)' for g in dict.fromkeys(named) if g not in known]
    problems += [f'rule 4 ("{g}" is used twice)' for g in dict.fromkeys(named) if named.count(g) > 1]
    families = sorted({known[g] for g in named if known.get(g)})
    if len(families) > 1:
        problems.append(f"rule 5 (it mixes the {' and '.join(families)} families)")
    if named and len(named) < len(got):
        problems.append('rule 6 (some points got "" and others an icon)')
    return ", ".join(problems)


def _describe(groups, notes=None) -> str:
    """The SLIDES as the model sees them: entries and points numbered from 0, each point a headline and a gist.
    With notes (on a second try), each slide's earlier choices and the rules they broke follow its points."""
    lines = [RETRY, ""] if notes else []
    lines.append("SLIDES")
    for n, members in enumerate(groups):
        title = " ".join(members[0].slide_title.split())[:160]
        lines.append(f"SLIDE {n} (deck slide {members[0].slide}" + (f', title: "{title}")' if title else ")"))
        for p, i in enumerate(members):
            head, *rest = [line.strip() for line in i.text.splitlines() if line.strip()] or [""]
            gist = " ".join(" ".join(rest).split())[:200]
            lines.append(f"  {p}. {' '.join(head.split())[:160]}" + (f" | {gist}" if gist else ""))
        if notes and _key(members[0]) in notes:
            lines.append(f"  {notes[_key(members[0])]}")
    return "\n".join(lines)


def _label(client, system, batch, notes=None, many=False) -> dict:
    """One request for a batch -> {item key: icon name or ""}; with many (the CLOSEST task), {item key: icon
    names, best first}. A reply cut off at MAX_TOKENS is retried as two halves, split between sets where it can be."""
    groups = _groups(batch)
    msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, system=system,
                                 messages=[{"role": "user", "content": _describe(groups, notes)}])
    if msg.stop_reason == "max_tokens" and len(batch) > 1:
        ends = [sum(len(g) for g in groups[:k]) for k in range(1, len(groups))] or [len(batch) // 2]
        half = min(ends, key=lambda e: abs(e - len(batch) / 2))
        return {**_label(client, system, batch[:half], notes, many), **_label(client, system, batch[half:], notes, many)}
    text = "".join(b.text for b in msg.content if b.type == "text")  # skips thinking blocks on newer models
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON in the reply, stop reason {msg.stop_reason}")
    data = json.loads(text[start:end + 1])
    entries, field = ("options", "icons") if many else ("choices", "icon")
    if not isinstance(data, dict) or not isinstance(data.get(entries), list):
        raise ValueError(f"no {entries} in the reply")
    picks = {}
    for c in data[entries]:
        try:
            n, p = int(c["slide"]), int(c["point"])
            item = groups[n][p] if n >= 0 and p >= 0 else None
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if item is not None:   # a whole library line sent back still names its icon: names have no "|"
            got = c.get(field) if isinstance(c.get(field), list) else [c.get(field)]
            names = [g.split("|")[0].strip() for g in got if isinstance(g, str)]
            picks[_key(item)] = names if many else (names[0] if names else "")
    return picks


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


def _extract_keywords(items, library):
    """Offline fallback (and for tests): the longest concept-map term found in the text, then the longest tag."""
    priority = list(library.concept_map)
    vocab = {t.lower() for i in library.icons.values() for t in i.tags} - set(priority)
    order = sorted(priority, key=len, reverse=True) + sorted(vocab, key=len, reverse=True)
    for i in items:
        low = i.text.lower()
        i.concepts = [v for v in order if v in low][:3]
        i.concept = i.concepts[0] if i.concepts else None
        i.icon_pick = None


def closest(items: list[Item], library, style: str, client=None) -> tuple[list[list[str]], list[str]]:
    """For items a designer will choose an icon for by hand: up to OPTIONS library icons closest in meaning to
    each, best first (they need not be perfect matches). Claude ranks them; without a key, where the API fails,
    or where it names nothing usable, tag matches on the item's words do. Returns the lists, in item order, and
    notes for the report."""
    if not items:
        return [], []
    pickable = {i.id for i in _pickable(library, style)}
    found, failed = {}, []
    if config.USE_LLM:
        import anthropic
        client = client or anthropic.Anthropic()
        system = [{"type": "text", "text": system_prompt(library, style), "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": CLOSEST.format(n=OPTIONS)}]   # same cached prefix as the picks

        def run(batch):
            try:
                return _label(client, system, batch, many=True), None
            except (anthropic.APIError, ValueError) as e:
                return {}, (len(batch), _failure(e)[0])

        with ThreadPoolExecutor(WORKERS) as pool:
            for got, failure in pool.map(run, _batches(items, CLOSEST_BATCH)):
                found.update(got)
                failed += [failure] if failure else []
    out = []
    for i in items:
        named = [n for n in dict.fromkeys(found.get(_key(i), [])) if n in pickable]
        out.append(named[:OPTIONS] or _closest_keywords(i, library, style, pickable))
    notes = [f"Claude couldn't rank icon options for {sum(n for n, _ in failed)} flagged placeholders "
             f"({'; '.join(dict.fromkeys(r for _, r in failed))}); keyword matching offered them"] if failed else []
    return out, notes


def _closest_keywords(item: Item, library, style: str, pickable: set) -> list[str]:
    """Tag matches on the item's ideas, its whole text, then each of its words: rough, best first."""
    words = re.findall(r"[^\W\d_]{3,}", item.text.lower())
    ids = [icon.id for idea in [*item.concepts, item.text, *words] if idea
           for icon in library.search(idea, style, arabic=item.rtl)]
    return [i for i in dict.fromkeys(ids) if i in pickable][:OPTIONS]
