"""Step 6: the report, as Markdown (for chat / the UI) and a dict (for the API)."""
from dataclasses import asdict

from .models import Report, label_of


def to_markdown(r: Report) -> str:
    s = r.settings
    verb = {"suggest": "Suggested", "replace": "Replaced placeholders with"}.get(r.mode, "Added")
    lines = [
        f"{verb} {len(r.picks)} icons in the {s.style} style, "
        f"deck color {s.color_slot} #{s.color_hex}, tiers {s.tiers}. {len(r.flags)} items flagged.", "",
        "| Slide | Item | Icon | Color | Why | Alternatives |", "| --- | --- | --- | --- | --- | --- |"]
    for p in sorted(r.picks, key=lambda p: p.item.slide):
        lines.append(f"| {p.item.slide} | {p.item.text[:50]} | {p.icon.id} | {p.color_slot} #{p.color_hex} | "
                     f"{p.reason} | {', '.join(p.alternatives) or '-'} |")
    if r.flags:
        lines += ["", "**Flagged (no icon added):**"]
        lines += [f"- Slide {f.item.slide}, '{f.item.text[:50]}': {f.reason}"
                  + (f" ({'closest icons, for a designer to choose' if f.spot else 'options'}: "
                     f"{', '.join(f.options)})" if f.options else "") for f in r.flags]
    lines += ["", "**Settings and gaps:**"] + [f"- {k}: {v}" for k, v in s.sources.items()] + [f"- {g}" for g in r.gaps]
    return "\n".join(lines)


def to_dict(r: Report) -> dict:
    """The report, with each item's label (label_of) so a designer's choices can be checked for one icon per label
    without reading the deck again (jobs.reuse_warnings, the page)."""
    d = asdict(r)
    for kind in ("picks", "flags"):
        for obj, out in zip(getattr(r, kind), d[kind]):
            out["item"]["label"] = label_of(obj.item.text)
    d["markdown"] = to_markdown(r)
    return d
