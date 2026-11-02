"""Smoke test with a tiny library and deck, plus checks on the real brand_icons/icons.json. Run: pytest -q"""
import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from app import config, library_edit, pipeline, report
from app.library import Library

BRAND_LIBRARY = Path(__file__).resolve().parent.parent / "icons"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keyword matching only: an API key in .env must not make the tests call Claude."""
    monkeypatch.setattr(config, "USE_LLM", False)

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="1.5"><path d="{d}"/></svg>'
ICONS = [
    ("finance-growth-chart", "finance", ["growth", "revenue", "increase"], ["decline"], "M3 21h18M7 15l4-4 3 3 5-6"),
    ("people-team", "people", ["team", "people", "talent"], [], "M9 8a3 3 0 1 0 0.1 0M3 20c0-3 3-5 6-5s6 2 6 5"),
    ("risk-shield", "risk", ["risk", "security", "protection"], [], "M12 3l8 3v6c0 5-4 8-8 9-4-1-8-4-8-9V6z"),
    ("partnership-handshake", "people", ["partnership"], [], "M4 12h16"),  # blocked by regional rule
]


def make_library(root):
    root.mkdir(parents=True)
    recs = []
    for icon_id, cat, tags, avoid, d in ICONS:
        (root / f"{icon_id}.svg").write_text(SVG.format(d=d))
        recs.append({"id": icon_id, "file": f"{icon_id}.svg", "status": "approved", "style": "outline",
                     "category": cat, "tags": tags, "avoid_for": avoid,
                     "preferred_for": ["growth"] if icon_id == "finance-growth-chart" else []})
    (root / "icons.json").write_text(json.dumps(recs))
    return Library(root)


def make_deck(path, texts):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    for i, t in enumerate(texts):
        tb = s.shapes.add_textbox(Inches(1 + i * 4), Inches(3), Inches(3.5), Inches(1))
        tb.text_frame.text = t
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
    prs.save(path)


def test_cards_get_icons(tmp_path):
    lib = make_library(tmp_path / "lib")
    make_deck(tmp_path / "deck.pptx", ["Revenue growth of 22%", "Team expansion", "Risk management"])
    r = pipeline.run(tmp_path / "deck.pptx", lib, "add")
    assert [p.icon.id for p in r.picks] == ["finance-growth-chart", "people-team", "risk-shield"]
    assert r.picks[0].reason == "concept map: growth"
    names = [s.name for s in Presentation(r.output_file).slides[0].shapes]
    assert "Icon - risk-shield" in names
    print(report.to_markdown(r))


def test_set_with_no_metaphor_is_flagged_whole(tmp_path):
    lib = make_library(tmp_path / "lib")
    make_deck(tmp_path / "deck.pptx", ["Revenue growth", "Strategic partnership", "Next steps"])
    r = pipeline.run(tmp_path / "deck.pptx", lib, "suggest")
    assert r.picks == [] and len(r.flags) == 3      # handshake blocked, 'next steps' has no metaphor
    assert r.output_file is None


def test_non_approved_icons_are_skipped(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "icons.json").write_text(json.dumps([
        {"id": "a", "file": "a.svg", "status": "approved", "style": "outline", "category": "x", "tags": ["a"]},
        {"id": "b", "file": "b.svg", "status": "deprecated", "style": "outline", "category": "x", "tags": ["b"]},
        {"id": "c", "file": "c.svg", "status": "sample", "style": "outline", "category": "x", "tags": ["c"]},
    ]))
    lib = Library(root)
    assert list(lib.icons) == ["a"]               # only approved icons can be picked
    assert list(lib.all) == ["a", "b", "c"]       # every record stays visible for editing


def test_brand_library_is_valid():
    """The checks a designer's edit to brand_icons/icons.json must pass (the same rules the Add icon form uses)."""
    raw = json.loads((BRAND_LIBRARY / "icons.json").read_text(encoding="utf-8"))
    errors = library_edit.validate_records(raw, BRAND_LIBRARY)
    assert not errors, "\n".join(errors)

    lib = Library(BRAND_LIBRARY)
    assert not lib.indexed_from_filenames and not lib.warnings, lib.warnings  # warnings = concept claimed twice
    assert lib.concept_map["growth"]["id"] == "increase-01"
    assert {i.style for i in lib.icons.values()} == {config.DEFAULT_STYLE}
    assert {i.family for i in lib.all.values()} == {"simple", "detailed"}     # every icon in one visual family


GOOD_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#000" d="M2 2h20v20H2z"/></svg>'
NEW_ICON = {"id": "speed-clock", "category": "operations", "description": "Clock face",
            "tags": "Speed; turnaround", "metaphor_strength": "strong"}


def test_add_icon_saves_svg_and_record(tmp_path):
    root = tmp_path / "lib"
    make_library(root)
    fields = {**NEW_ICON, "tags_ar": "سُرعة، وقت", "preferred_for": "speed, deadline", "rtl_flip": "true",
              "family": "Simple"}
    record, warnings = library_edit.add_icon(root, fields, GOOD_SVG)

    assert (root / "speed-clock.svg").read_bytes() == GOOD_SVG
    assert record["tags"] == ["speed", "turnaround", "deadline"]   # lowercased, house picks added as tags
    assert record["tags_ar"] == ["سرعة", "وقت"]                     # Arabic comma split, diacritics removed
    assert record["rtl_flip"] is True and record["status"] == "approved" and record["style"] == "outline"
    assert record["family"] == "simple"
    assert any("license" in w for w in warnings)
    lib = Library(root)
    assert lib.concept_map["deadline"]["id"] == "speed-clock"


@pytest.mark.parametrize("change, svg, expected", [
    ({"id": "people-team"}, GOOD_SVG, "id is used twice"),
    ({"id": "Speed Clock!"}, GOOD_SVG, "lowercase words joined by hyphens"),
    ({"tags": ""}, GOOD_SVG, "missing tags"),
    ({"tags": "speed; thumbs up"}, GOOD_SVG, "regional block list"),
    ({"preferred_for": "growth"}, GOOD_SVG, "already the house pick for finance-growth-chart"),
    ({"pairs_with": "no-such-icon"}, GOOD_SVG, "unknown ids"),
    ({}, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "scripts"),
    ({}, b'<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="x()"/></svg>', "script attribute"),
    ({}, b'<svg xmlns="http://www.w3.org/2000/svg"><image href="photo.png"/></svg>', "raster image"),
    ({}, b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"', "not a valid SVG"),
    ({}, b"not an icon at all", "unsupported file type"),
    ({}, b"%PDF-1.7 ...", "export the icon as SVG"),
])
def test_add_icon_rejects_bad_input_and_writes_nothing(tmp_path, change, svg, expected):
    root = tmp_path / "lib"
    make_library(root)
    index_before = (root / "icons.json").read_text()
    files_before = sorted(p.name for p in root.iterdir())
    with pytest.raises(library_edit.InvalidIcon) as e:
        library_edit.add_icon(root, {**NEW_ICON, **change}, svg)
    assert any(expected in err for err in e.value.errors), e.value.errors
    assert (root / "icons.json").read_text() == index_before
    assert sorted(p.name for p in root.iterdir()) == files_before


def test_svg_warnings_do_not_block():
    two_colors = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#000" d="M0 0h1"/><path stroke="#f00" d="M0 0h1"/></svg>'
    errors, warnings = library_edit.check_svg(two_colors)
    assert errors == []
    assert any("2 colors" in w for w in warnings) and any("viewBox" in w for w in warnings)


def raster_icon(fmt="PNG", px=256, background=None):
    """A ring and a bar: dark on transparent (PNG) or on a plain background (JPG)."""
    from io import BytesIO
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (px, px), background or (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((px * 0.1, px * 0.1, px * 0.7, px * 0.7), outline=(31, 58, 138, 255), width=max(2, px // 16))
    d.rectangle((px * 0.6, px * 0.75, px * 0.9, px * 0.85), fill=(31, 58, 138, 255))
    out = BytesIO()
    (im.convert("RGB") if fmt == "JPEG" else im).save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt, background, ext", [("PNG", None, "png"), ("JPEG", (255, 255, 255, 255), "jpg")])
def test_raster_upload_is_traced_to_svg(tmp_path, fmt, background, ext):
    root = tmp_path / "lib"
    make_library(root)
    data = raster_icon(fmt, background=background)
    record, warnings = library_edit.add_icon(root, {**NEW_ICON, "id": "ring-bar"}, data)

    svg = (root / "ring-bar.svg").read_bytes()
    assert record["file"] == "ring-bar.svg" and svg.startswith(b"<svg") and b"viewBox" in svg
    assert library_edit.check_svg(svg) == ([], [])                 # one color, viewBox, nothing unsafe
    assert (root / "originals" / f"ring-bar.{ext}").read_bytes() == data
    assert warnings[0].startswith(f"traced from {fmt}")
    assert Library(root).get("ring-bar") is not None


def test_raster_warnings_and_blank_image():
    from io import BytesIO
    from PIL import Image
    svg, fmt, warnings = library_edit.vectorize.raster_to_svg(raster_icon(px=64))
    assert fmt == "PNG" and any("64x64" in w for w in warnings)  # low resolution is traced, with a warning

    blank = BytesIO()
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(blank, "PNG")
    with pytest.raises(ValueError, match="no icon shape found"):
        library_edit.vectorize.raster_to_svg(blank.getvalue())


def test_edit_icon_tags_and_house_picks(tmp_path):
    root = tmp_path / "lib"
    make_library(root)
    others_before = [r for r in json.loads((root / "icons.json").read_text()) if r["id"] != "people-team"]
    record, warnings = library_edit.update_icon(root, "people-team", {
        "tags": "team; talent; workforce", "tags_ar": "فَريق، موظفون", "preferred_for": "workforce",
        "description": "Two people", "metaphor_strength": "strong", "rtl_flip": "false"})

    assert record["tags"] == ["team", "talent", "workforce"] and record["tags_ar"] == ["فريق", "موظفون"]
    assert record["file"] == "people-team.svg" and record["category"] == "people"  # untouched fields kept
    saved = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    assert [r for r in saved if r["id"] != "people-team"] == others_before         # other icons untouched
    assert Library(root).concept_map["workforce"]["id"] == "people-team"
    assert any("license" in w for w in warnings)


def test_partial_edit_keeps_other_fields_and_id_and_file_are_fixed(tmp_path):
    root = tmp_path / "lib"
    make_library(root)
    library_edit.update_icon(root, "risk-shield", {"contexts": "banking", "rtl_flip": "true"})
    record, _ = library_edit.update_icon(root, "risk-shield", {"tags": "risk; security; safety",
                                                               "id": "renamed", "file": "other.svg"})
    assert record["id"] == "risk-shield" and record["file"] == "risk-shield.svg"
    assert record["contexts"] == ["banking"] and record["rtl_flip"] is True and record["tags"][-1] == "safety"


def test_deprecating_an_icon_retires_it(tmp_path):
    root = tmp_path / "lib"
    make_library(root)
    library_edit.update_icon(root, "finance-growth-chart", {"status": "deprecated"})
    lib = Library(root)
    assert "finance-growth-chart" not in lib.icons and "growth" not in lib.concept_map
    assert lib.all["finance-growth-chart"].status == "deprecated"


@pytest.mark.parametrize("icon_id, change, expected", [
    ("people-team", {"preferred_for": "growth"}, "already the house pick for finance-growth-chart"),
    ("people-team", {"tags": "team; thumbs up"}, "regional block list"),
    ("people-team", {"pairs_with": "no-such-icon"}, "unknown ids"),
    ("people-team", {"status": "archived"}, "status must be"),
    ("people-team", {"family": "bold glyphs"}, "family must be one lowercase word"),
    ("people-team", {"tags": ""}, "missing tags"),
])
def test_bad_edit_is_rejected_and_writes_nothing(tmp_path, icon_id, change, expected):
    root = tmp_path / "lib"
    make_library(root)
    before = (root / "icons.json").read_text()
    with pytest.raises(library_edit.InvalidIcon) as e:
        library_edit.update_icon(root, icon_id, change)
    assert any(expected in err for err in e.value.errors), e.value.errors
    assert (root / "icons.json").read_text() == before


def test_edit_unknown_icon(tmp_path):
    root = tmp_path / "lib"
    make_library(root)
    with pytest.raises(KeyError):
        library_edit.update_icon(root, "no-such-icon", {"tags": "x"})


def test_house_pick_clash_names_the_icon_being_edited(tmp_path):
    root = tmp_path / "lib"
    make_library(root)                                       # file order: ..., people-team, risk-shield
    library_edit.update_icon(root, "risk-shield", {"preferred_for": "security"})
    with pytest.raises(library_edit.InvalidIcon) as e:
        library_edit.update_icon(root, "people-team", {"preferred_for": "security"})
    assert e.value.errors == ["people-team: 'security' is already the house pick for risk-shield"]


def sent_points(content):
    """(slide entry, point, text) for every point in a request's SLIDES."""
    points, entry = [], None
    for line in content.splitlines():
        m = __import__("re").match(r"SLIDE (\d+) |\s+(\d+)\. (.*)$", line)
        if m and m.group(1):
            entry = int(m.group(1))
        elif m:
            points.append((entry, int(m.group(2)), m.group(3)))
    return points


class FakeClaude:
    """Stands in for anthropic.Anthropic(): picks one icon (by default the same one) for every point it is sent;
    asked for the closest icons (a second system block), lists options(text) for each point."""
    def __init__(self, icon="finance-growth-chart", fail=None, max_items=None, thinking=False, text=None, options=None,
                 refuse=False, fit=None):
        self.beta = self.messages = self
        self.calls, self.systems, self.sent = 0, [], {}
        self.icon, self.fail, self.max_items, self.thinking, self.text = icon, fail, max_items, thinking, text
        self.options, self.refuse, self.fit = options, refuse, fit   # fit(text): how sure, 1-5 (None: not given)

    def create(self, **kw):
        from types import SimpleNamespace as NS
        self.calls, self.sent = self.calls + 1, kw
        if self.fail:
            raise self.fail
        if self.refuse is not False:   # declined, fallback included; refuse names the category (or None)
            return NS(stop_reason="refusal", stop_details=NS(category=self.refuse), content=[])
        points = sent_points(kw["messages"][0]["content"])
        if self.max_items and len(points) > self.max_items:   # reply cut off mid-JSON
            return NS(stop_reason="max_tokens", content=[NS(type="text", text='{"choices":[{"slide":0,"po')])
        icon = self.icon if callable(self.icon) else (lambda text: self.icon)
        self.systems.append(kw["system"])
        if len(kw["system"]) > 1:   # the closest icons for flagged placeholders
            near = self.options or (lambda text: [icon(text)])
            data = {"options": [{"slide": s, "point": p, "icons": near(t)} for s, p, t in points]}
        else:
            data = {"choices": [{"slide": s, "point": p, "icon": icon(t), **({"fit": self.fit(t)} if self.fit else {})}
                                for s, p, t in points]}
        text = self.text if self.text is not None else "Choices:\n" + json.dumps(data)
        blocks = [NS(type="thinking", thinking="")] if self.thinking else []
        return NS(stop_reason="end_turn", content=blocks + [NS(type="text", text=text)])


def slide_items(n):
    from app.models import Item
    return [Item(slide=1 + k // 10, shape_id=k, text=f"Revenue growth in region {k}", kind="card",
                 left=0, top=0, w=1, h=1) for k in range(n)]


def test_llm_picks_for_large_decks_in_batches(tmp_path):
    from app import concepts
    items, fake = slide_items(130), FakeClaude(icon=" finance-growth-chart ")
    assert concepts._extract_llm(items, make_library(tmp_path / "lib"), "outline", client=fake) == []
    assert fake.calls == 3 and {i.icon_pick for i in items} == {"finance-growth-chart"}   # 60 + 60 + 10, trimmed
    assert fake.sent["model"] == "claude-sonnet-5-5" and fake.sent["output_config"] == {"effort": "low"}
    assert fake.sent["fallbacks"] == "default" and fake.sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in fake.sent and "thinking" not in fake.sent       # adaptive thinking, the default


@pytest.mark.parametrize("fake", [FakeClaude(max_items=20), FakeClaude(thinking=True)])
def test_cut_off_replies_are_split_and_thinking_blocks_skipped(fake, tmp_path):
    from app import concepts
    items = slide_items(130)
    assert concepts._extract_llm(items, make_library(tmp_path / "lib"), "outline", client=fake) == []
    assert all(i.icon_pick == i.concept == "finance-growth-chart" for i in items)


def test_api_failures_fall_back_to_keywords_with_a_note(tmp_path):
    import anthropic
    import httpx2
    from app import concepts
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [
        (FakeClaude(fail=anthropic.APIConnectionError(request=request)), "couldn't reach the Claude API", 3),
        (FakeClaude(fail=anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=request),
                                                       body=None)), "API key was rejected", 1),
        (FakeClaude(text="Sorry, I can't help with that."), "unreadable reply", 3),
        (FakeClaude(refuse="general_harms"), "it declined them under its general harms safeguards", 3),
        (FakeClaude(refuse=None), "(it declined them)", 3),
    ]
    lib = make_library(tmp_path / "lib")
    for fake, reason, calls in cases:
        items = slide_items(130)
        notes = concepts._extract_llm(items, lib, "outline", client=fake)
        assert len(notes) == 1 and reason in notes[0] and "130 of 130" in notes[0], notes
        assert fake.calls == calls                                    # a rejected key stops after one request
        assert all(i.concept == "growth" and i.icon_pick is None for i in items)   # keyword matching stood in


def test_add_only_the_chosen_suggestions(tmp_path):
    lib = make_library(tmp_path / "lib")
    deck_path = tmp_path / "deck.pptx"
    make_deck(deck_path, ["Revenue growth of 22%", "Team expansion", "Risk management"])
    saved = json.loads(json.dumps(report.to_dict(pipeline.run(deck_path, lib, "suggest"))))  # as stored with the job
    assert [p["icon"]["id"] for p in saved["picks"]] == ["finance-growth-chart", "people-team", "risk-shield"]

    out, skipped = pipeline.insert(deck_path, lib, pipeline.placements_from_report(saved, [0, 2]),
                                   saved["settings"])
    names = [s.name for s in Presentation(out).slides[0].shapes]
    assert "Icon - finance-growth-chart" in names and "Icon - risk-shield" in names
    assert "Icon - people-team" not in names and skipped == []
    assert len(Presentation(deck_path).slides[0].shapes) == 3                  # original untouched

    library_edit.update_icon(tmp_path / "lib", "risk-shield", {"status": "deprecated"})
    out, skipped = pipeline.insert(deck_path, Library(tmp_path / "lib"),
                                   pipeline.placements_from_report(saved, [0, 1, 2]), saved["settings"])
    assert skipped == ["risk-shield"]                                           # retired since the suggestion
    assert sum(s.name.startswith("Icon - ") for s in Presentation(out).slides[0].shapes) == 2


# --- icon colors: always a theme slot, chosen for contrast with what is behind the icon -------------------

def test_brand_svg_is_recolored_and_bound_to_the_theme():
    from io import BytesIO
    from PIL import Image
    from app.icon_tools import recolor_svg, svg_to_png
    out = recolor_svg((BRAND_LIBRARY / "Analytics.svg").read_bytes(), "A32020", "accent1").decode()
    assert "b6edf3" not in out.lower() and "cls-1" not in out                  # the CSS color is gone
    assert out.count('class="MsftOfcThm_Accent1_Fill_v2"') == 3 and ".MsftOfcThm_Accent1_Fill_v2{fill:#A32020;}" in out
    im = Image.open(BytesIO(svg_to_png(out.encode()))).convert("RGBA")
    solid = [c for n, c in im.getcolors(1 << 20) if c[3] == 255]
    assert solid and all(c[:3] == (163, 32, 32) for c in solid)                 # renders in the theme color


def test_outline_strokes_keep_their_css_width_and_caps():
    from lxml import etree
    from app.icon_tools import recolor_svg
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><style>.a{fill:none;stroke:#000;'
           b'stroke-width:2px;stroke-linecap:round}</style><path class="a" d="M2 2h20"/>'
           b'<g fill="none" stroke="#333" stroke-width="1.5"><path d="M2 9h20"/></g></svg>')
    paths = etree.fromstring(recolor_svg(svg, "1F497D", "tx2")).findall(".//{http://www.w3.org/2000/svg}path")
    assert [(p.get("fill"), p.get("stroke"), p.get("stroke-width"), p.get("class")) for p in paths] == [
        ("none", "#1F497D", "2px", "MsftOfcThm_Text2_Stroke_v2"), ("none", "#1F497D", "1.5", "MsftOfcThm_Text2_Stroke_v2")]
    assert paths[0].get("stroke-linecap") == "round"


def test_inserted_icon_keeps_its_proportions():
    from app.icon_tools import insert_icon
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    pic = insert_icon(prs, 1, (BRAND_LIBRARY / "Checklist.svg").read_bytes(), 1.0, 1.0, 1.0, "4F81BD", theme_slot="accent1")
    assert pic.width == Inches(1.0) and abs(pic.width / pic.height - 78.28 / 50.85) < 0.01
    assert abs((pic.top + pic.height / 2) - Inches(1.5)) < 2                    # centered in its 1in slot


def color_slide(card=None, text=None, background=None, group=False):
    """A slide with an optional theme-filled card (optionally inside a group) behind a text box."""
    from pptx.enum.shapes import MSO_SHAPE
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    if background is not None:
        slide.background.fill.solid()
        slide.background.fill.fore_color.theme_color = background
    if card is not None:
        target = slide.shapes.add_group_shape() if group else slide
        rect = target.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(3), Inches(3))
        rect.fill.solid()
        rect.fill.fore_color.theme_color = card
    ids = []
    for x in (1.2, 6.2):
        tb = slide.shapes.add_textbox(Inches(x), Inches(2.5), Inches(2.6), Inches(1))
        tb.text_frame.text = "Revenue growth"
        if text is not None:
            tb.text_frame.paragraphs[0].runs[0].font.color.theme_color = text
        ids.append(tb.shape_id)
    return prs, ids


def icon_pick(shape_id, x, set_id=None):
    """A pick whose 0.55in icon sits at x, 1.5in from the top (over the card when x is 1-4in)."""
    from app.models import IconRecord, Item, Pick
    item = Item(slide=1, shape_id=shape_id, text="Revenue growth", kind="card", left=x, top=2.5, w=2.6, h=1, set_id=set_id)
    return Pick(item, IconRecord("x", "x.svg", "outline", "c", []), "", [], left=x, top=1.5, size=0.55)


@pytest.mark.parametrize("card, text, background, group, expected", [
    (None, None, None, False, "accent1"),          # white slide: the deck color has enough contrast
    ("ACCENT_1", "BACKGROUND_1", None, False, "bg1"),   # accent card with white text: the text color
    ("ACCENT_1", None, None, False, "tx1"),        # accent card, default text: the theme's dark color
    ("ACCENT_1", None, None, True, "tx1"),         # same card inside a group
    (None, None, "TEXT_2", False, "bg1"),          # dark slide background: the theme's light color
])
def test_icon_color_depends_on_what_is_behind_it(card, text, background, group, expected):
    from pptx.enum.dml import MSO_THEME_COLOR
    from app import colors
    from app.icon_tools import THEME_CLASS
    t = lambda name: getattr(MSO_THEME_COLOR, name) if name else None
    prs, ids = color_slide(t(card), t(text), t(background), group)
    pick = icon_pick(ids[0], 2.2)
    colors.assign(prs, [pick], "accent1")
    assert pick.color_slot == expected and pick.color_slot in THEME_CLASS, pick.color_note
    assert pick.color_hex == colors.Theme(prs.slide_masters[0]).slot_hex(expected)
    behind = colors.Theme(prs.slide_masters[0])
    assert colors.contrast(pick.color_hex, colors.behind(behind, prs.slides[0], 2.475, 1.775)[0]) >= 3


def test_a_row_of_cards_shares_one_color_that_works_on_every_card():
    from pptx.enum.dml import MSO_THEME_COLOR
    from app import colors
    prs, ids = color_slide(MSO_THEME_COLOR.ACCENT_1)            # first card is accent1, second sits on white
    row = [icon_pick(ids[0], 2.2, "row"), icon_pick(ids[1], 7.2, "row")]
    colors.assign(prs, row, "accent1")
    assert {p.color_slot for p in row} == {"tx1"}               # accent1 fails on card 1, white text fails on card 2


def test_see_through_cards_and_plain_black_or_white_text_use_the_theme():
    from lxml import etree
    from pptx.enum.dml import MSO_THEME_COLOR
    from app import colors
    prs, ids = color_slide(MSO_THEME_COLOR.ACCENT_1)
    theme = colors.Theme(prs.slide_masters[0])
    card = next(s for s in prs.slides[0].shapes if s.shape_type == 1)       # the autoshape card
    clr = card._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}schemeClr")
    etree.SubElement(clr, "{http://schemas.openxmlformats.org/drawingml/2006/main}alpha", val="15000")  # 15% opacity
    bg, where = colors.behind(theme, prs.slides[0], 2.475, 1.775)
    assert where == "the shape behind it" and bg == colors._blend("4F81BD", "FFFFFF", 0.15)  # a pale tint, not accent1
    pick = icon_pick(ids[0], 2.2)
    colors.assign(prs, [pick], "accent1")
    assert pick.color_slot == "accent1"                                      # the deck color reads fine on the tint

    tb = prs.slides[0].shapes[-2]
    tb.text_frame.paragraphs[0].runs[0].font.color.rgb = __import__("pptx").dml.color.RGBColor(0xFF, 0xFF, 0xFF)
    assert colors.text_slot(theme, prs.slides[0], pick.item) == "bg1"       # plain white text is the theme's bg1


def test_mostly_opaque_card_over_a_picture_counts_as_the_card():
    from io import BytesIO
    from lxml import etree
    from PIL import Image
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from app import colors
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    png = BytesIO()
    Image.new("RGB", (8, 8), (240, 120, 40)).save(png, "PNG")
    slide.shapes.add_picture(png, Inches(1), Inches(1), Inches(3), Inches(3))          # textured card image
    for alpha, expected in (("80000", "4F81BD"), ("30000", colors.IMAGE)):
        card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(3), Inches(3))
        card.fill.solid()
        card.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
        clr = card._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}schemeClr")
        etree.SubElement(clr, "{http://schemas.openxmlformats.org/drawingml/2006/main}alpha", val=alpha)
        theme = colors.Theme(prs.slide_masters[0])
        assert colors.behind(theme, slide, 2, 2)[0] == expected   # 80% opaque reads as accent1; a 30% tint stays unknown
        card._element.getparent().remove(card._element)


# --- item detection, placement that never covers content, existing icons' color --------------------------

def boxes_slide(specs):
    """A blank 13.33x7.5 slide with text boxes: (left, top, width, height, text) in inches. Paragraphs are
    separated by a newline; a paragraph starting with "• " gets a real bullet."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    ids = []
    for left, top, w, h, text in specs:
        tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(w), Inches(h))
        tb.text_frame.word_wrap = True
        tb.text_frame.text = text.replace("• ", "")
        for para, line in zip(tb.text_frame.paragraphs, text.splitlines()):
            if line.startswith("• "):
                ppr = para._p.get_or_add_pPr()
                ppr.append(ppr.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}buChar", {"char": "•"}))
        ids.append(tb.shape_id)
    return prs, ids


def test_item_detection_skips_numbers_sources_and_footers():
    from app import deck
    prs, _ = boxes_slide([(1, 3, 3.5, 1, "Revenue growth"), (5, 3, 3.5, 1, "Team expansion"),
                          (9, 3, 3.5, 1, "Risk management"), (12.6, 0.3, 0.5, 0.4, "15"),
                          (1, 6.2, 6, 0.4, "Source: Team analysis"), (1, 7.0, 4, 0.3, "Firm | Client name")])
    items = deck.read_items(prs)
    assert [(i.text, i.kind) for i in items] == [("Revenue growth", "card"), ("Team expansion", "card"),
                                                ("Risk management", "card")]
    assert len({i.set_id for i in items}) == 1


def test_headings_and_their_descriptions_are_not_one_row():
    from app import deck
    long = "Increased adoption of emerging technology, including automation, generative AI and cloud computing"
    prs, ids = boxes_slide([(0.5, 1.5, 3.5, 0.9, "Technological innovation"), (4.2, 1.5, 8.6, 0.9, f"• {long}\n• Focus on acquisitions"),
                            (0.5, 2.6, 3.5, 0.9, "Evolution of delivery models"), (4.2, 2.6, 8.6, 0.9, "• Becoming a trusted partner\n• Remote work")])
    items = deck.read_items(prs)
    heads = [i for i in items if i.kind == "card"]
    assert [i.text for i in heads] == ["Technological innovation", "Evolution of delivery models"]
    assert heads[0].set_id == heads[1].set_id and heads[0].set_id.startswith("s1-set")   # one component of headings
    assert all(h.rank == 1 for h in heads) and all(b.rank == 3 for b in items if b.kind == "list")
    bullets = [i for i in items if i.kind == "list" and i.shape_id == ids[1]]
    assert [b.line for b in bullets] == [0, deck.text_lines(long, 8.6, 18.0)] and bullets[1].line >= 2


def card_picks(prs, specs_ids, kind="card", set_id="row"):
    from app.models import IconRecord, Item, Pick
    out = []
    for shape_id in specs_ids:
        sh = next(s for s in prs.slides[0].shapes if s.shape_id == shape_id)
        it = Item(slide=1, shape_id=shape_id, text=sh.text_frame.text, kind=kind, left=sh.left / 914400,
                  top=sh.top / 914400, w=sh.width / 914400, h=sh.height / 914400, set_id=set_id)
        out.append(Pick(it, IconRecord("x", "x.svg", "outline", "c", []), "", []))
    return out


def tier_settings():
    from app.models import DeckSettings
    return DeckSettings("outline", "4F81BD", {"list": 0.3, "card": 0.55, "column": 0.55, "hero": 1.2}, {})


def test_headings_get_their_icon_right_beside_the_text():
    from app import placement
    prs, ids = boxes_slide([(2, 1.5, 3, 0.8, "Revenue growth"), (6, 1.5, 3, 0.8, "Team expansion"),
                            (2, 2.4, 3, 0.8, "Risk management"), (6, 2.4, 3, 0.8, "Data security")])
    grid = card_picks(prs, ids, set_id="grid")                            # one component of four headings
    assert placement.place(grid, tier_settings(), prs) == []
    assert {p.position for p in grid} == {"beside"}
    assert all(p.left + p.size <= p.item.left + 0.1 for p in grid)       # just left of where the text starts


def test_one_icon_set_per_slide_goes_to_the_best_ranked_component():
    from app import placement
    prs, ids = boxes_slide([(2, 1.5, 3, 0.8, "Revenue growth"), (6, 1.5, 3, 0.8, "Team expansion"),
                            (2, 4.0, 3, 0.8, "Risk management"), (6, 4.0, 3, 0.8, "Data security")])
    labels, bodies = card_picks(prs, ids[:2], set_id="labels"), card_picks(prs, ids[2:], set_id="bodies")
    for p in bodies:
        p.item.rank = 2                                                    # longer text ranks below short labels
    flags = placement.place(bodies + labels, tier_settings(), prs)
    assert {f.item.text for f in flags} == {"Risk management", "Data security"}
    assert all(f.reason.startswith("one icon set per slide") for f in flags) and all(p.position for p in labels)


def test_a_set_with_no_free_spot_is_flagged_as_a_whole():
    from app import placement
    prs, ids = boxes_slide([(0.5, 1.5, 3, 0.8, "Revenue growth"), (4, 1.5, 3, 0.8, "Team expansion"),
                            (0.5, 2.4, 3, 0.8, "Risk management"), (4, 2.4, 3, 0.8, "Data security")])
    grid = card_picks(prs, ids, set_id="grid")     # beside: the left column hits the slide edge; above: row 1's text
    flags = placement.place(grid, tier_settings(), prs)
    assert len(flags) == 4 and all(f.reason.startswith("no room for the icon") for f in flags)   # none, not some


def test_an_icon_may_sit_on_a_card_but_not_on_its_text():
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from app import placement
    prs, ids = boxes_slide([(2.2, 2.4, 3.6, 0.8, "Revenue growth")])
    card = prs.slides[0].shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(2), Inches(1), Inches(4), Inches(2.5))
    card.fill.solid()
    card.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
    sp_tree = prs.slides[0].shapes._spTree
    sp_tree.remove(card._element)
    sp_tree.insert(2, card._element)                                       # the card sits behind the text box
    picks = card_picks(prs, ids, set_id=None)
    assert placement.place(picks, tier_settings(), prs) == [] and picks[0].position == "above"

    card.text_frame.text = "WHAT DO WE DO, WHERE AND HOW WELL?"           # a heading long enough to wrap down
    card.text_frame.vertical_anchor = __import__("pptx").enum.text.MSO_ANCHOR.TOP
    card.text_frame.paragraphs[0].runs[0].font.size = __import__("pptx").util.Pt(28)
    flags = placement.place(card_picks(prs, ids, set_id=None), tier_settings(), prs)
    assert flags and "WHAT DO WE DO" in flags[0].reason                    # to where the icon would go


def test_existing_icons_set_the_deck_color_when_they_agree():
    from io import BytesIO
    from PIL import Image, ImageDraw
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from app import colors, deck
    from app.icon_tools import insert_icon
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    theme = colors.Theme(prs.slide_masters[0])
    svg = (BRAND_LIBRARY / "Target.svg").read_bytes()
    for x in (1, 2, 3):                                                   # three SVG icons bound to tx2
        insert_icon(prs, 1, svg, x, 1, 0.5, theme.slot_hex("tx2"), theme_slot="tx2")
    png = BytesIO()                                                       # a PNG icon drawn in accent2
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((8, 8, 56, 56), fill="#" + theme.slot_hex("accent2"))
    im.save(png, "PNG")
    prs.slides[0].shapes.add_picture(png, Inches(4), Inches(1), Inches(0.5), Inches(0.5))
    group = prs.slides[0].shapes.add_group_shape()                        # a native icon built from shapes
    part = group.shapes.add_shape(MSO_SHAPE.OVAL, Inches(5), Inches(1), Inches(0.5), Inches(0.5))
    part.fill.solid()
    part.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_3
    found = deck.existing_icons(prs)
    assert [e["color_slot"] for e in found] == ["tx2", "tx2", "tx2", "accent2", "accent3"]

    class Lib:  # detect_settings only needs the style guide and icon lookups
        style_guide = {}
        get = staticmethod(lambda _id: None)
    settings = deck.detect_settings(prs, Lib, found[:3])
    assert settings.color_slot == "tx2" and "existing icons (3 of 3" in settings.sources["color"]
    settings = deck.detect_settings(prs, Lib, found[:3] + [found[3]] * 2)   # 3 of 5 agree: below 2/3
    assert settings.color_slot == "accent1" and "mixed colors" in settings.sources["color"]


def test_a_heading_with_its_description_is_one_card_and_a_header_strip_is_no_column():
    from app import deck
    prs, _ = boxes_slide([(1, 1.5, 4, 1.2, "Route 1\nProcesses are standardized, then moved"),
                          (1, 2.9, 4, 1.2, "Route 2\nProcesses are moved, then standardized"),
                          (7, 4.5, 4, 0.4, "Discussion sessions"), (7, 5.0, 4, 1.2, "Conduct sessions with specialists")])
    items = deck.read_items(prs)
    assert [(i.text.splitlines()[0], i.kind) for i in items] == [("Route 1", "card"), ("Route 2", "card")]


def test_an_icon_never_sits_on_a_divider_line():
    from pptx.enum.shapes import MSO_CONNECTOR
    from app import placement
    prs, ids = boxes_slide([(3, 1.5, 3, 0.8, "Revenue growth"), (3, 2.4, 3, 0.8, "Risk management")])
    prs.slides[0].shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(2.85), Inches(1), Inches(2.85), Inches(4))
    picks = card_picks(prs, ids, set_id="col")
    flags = placement.place(picks, tier_settings(), prs)   # above: covers the first heading; beside: the line
    assert flags and all("a line" in f.reason or "text" in f.reason for f in flags)


def test_hero_icons_only_when_the_deck_already_uses_them(tmp_path):
    lib = make_library(tmp_path / "lib")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(4), Inches(3.5), Inches(5), Inches(1)).text_frame.text = "Revenue growth"
    prs.save(tmp_path / "divider.pptx")
    r = pipeline.run(tmp_path / "divider.pptx", lib, "suggest")
    assert r.picks == [] and r.flags[0].reason.startswith("no hero icon")


def test_a_hero_icon_already_on_a_divider_allows_hero_icons(tmp_path):
    from app import colors
    from app.icon_tools import insert_icon
    lib = make_library(tmp_path / "lib")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for text in ("Revenue growth", "Risk management"):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(4), Inches(3.5), Inches(5), Inches(1)).text_frame.text = text
    theme = colors.Theme(prs.slide_masters[0])
    insert_icon(prs, 1, (BRAND_LIBRARY / "Target.svg").read_bytes(), 6, 1.6, 1.2, theme.slot_hex("accent1"), theme_slot="accent1")
    prs.save(tmp_path / "dividers.pptx")
    r = pipeline.run(tmp_path / "dividers.pptx", lib, "suggest")
    assert ("Risk management", "hero") in [(p.item.text, p.item.kind) for p in r.picks]
    assert not any(f.reason.startswith("no hero icon") for f in r.flags)   # the deck uses hero icons: allowed


def test_an_icon_never_lands_inside_someone_elses_card_or_picture():
    from io import BytesIO
    from PIL import Image
    from app import placement
    prs, ids = boxes_slide([(3, 3.2, 3, 0.8, "Revenue growth")])
    png = BytesIO()
    Image.new("RGB", (40, 30), (200, 200, 200)).save(png, "PNG")
    prs.slides[0].shapes.add_picture(png, Inches(2.5), Inches(1.0), Inches(4), Inches(2.1))   # a screenshot above
    picks = card_picks(prs, ids, set_id=None)
    assert placement.place(picks, tier_settings(), prs) == []
    assert picks[0].position == "beside"                  # above would land on the picture, so beside it goes
    assert picks[0].top >= 3.1 - 0.03                     # clear of the picture's bottom edge


def diagram_slide(shape_kind, specs, fill="ACCENT_1", align_center=True, font=14, anchor_middle=True):
    """Filled shapes carrying their own text: (left, top, width, height, text) in inches."""
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for left, top, w, h, text in specs:
        sh = slide.shapes.add_shape(getattr(MSO_SHAPE, shape_kind), Inches(left), Inches(top), Inches(w), Inches(h))
        sh.fill.solid()
        sh.fill.fore_color.theme_color = getattr(MSO_THEME_COLOR, fill)
        sh.text_frame.text = text
        sh.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE if anchor_middle else MSO_ANCHOR.TOP
        for p in sh.text_frame.paragraphs:
            p.alignment = PP_ALIGN.CENTER if align_center else PP_ALIGN.LEFT
            for r in p.runs:
                r.font.size, r.font.bold = Pt(font), True
    return prs


def test_all_nodes_of_a_diagram_are_one_set_and_get_icons_inside_above_their_labels():
    from app import deck, placement
    from app.models import IconRecord, Pick
    hexes = [(4.6, 2.3, 1.4, 1.25, "Technology & Data"), (6.1, 2.3, 1.4, 1.25, "Strategy & Vision"),
             (3.8, 3.6, 1.4, 1.25, "People & Culture"), (6.9, 3.6, 1.4, 1.25, "Organization & Governance"),
             (4.6, 4.9, 1.4, 1.25, "Process Architecture"), (6.1, 4.9, 1.4, 1.25, "Global Footprint")]
    prs = diagram_slide("HEXAGON", hexes, font=12)
    items = deck.read_items(prs)
    assert len(items) == 6 and len({i.set_id for i in items}) == 1 and all(i.in_shape and i.rank == 0 for i in items)
    picks = [Pick(i, IconRecord("x", "x.svg", "outline", "c", []), "", []) for i in items]
    assert placement.place(picks, tier_settings() if False else __import__("app.models", fromlist=["x"]).DeckSettings(
        "outline", "4F81BD", {"list": 0.3, "card": 0.3, "column": 0.3, "hero": 1.2}, {}), prs) == []
    for p in picks:                                   # inside its hexagon, centered over its label
        it = p.item
        assert p.position == "above" and it.left <= p.left and p.left + p.size <= it.left + it.w
        assert it.top <= p.top and p.top + p.size <= it.tx_top
        assert abs((p.left + p.size / 2) - (it.left + it.w / 2)) < 0.05


def test_a_heading_on_a_panel_gets_its_icon_beside_it_on_the_panel():
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Pt
    from app import deck, placement
    from app.models import IconRecord, Pick
    prs, _ = boxes_slide([])
    slide = prs.slides[0]
    for x, color in ((5.5, MSO_THEME_COLOR.BACKGROUND_2), (9.0, MSO_THEME_COLOR.ACCENT_2)):
        panel = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(1.5), Inches(3.4), Inches(4.5))
        panel.fill.solid()
        panel.fill.fore_color.theme_color = color
        head = slide.shapes.add_textbox(Inches(x + 0.7), Inches(1.6), Inches(2.0), Inches(0.8))   # wraps to 2 lines
        head.text_frame.text = "Enabling technologies" if x < 9 else "Business solutions"
        head.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        head.text_frame.paragraphs[0].runs[0].font.size, head.text_frame.paragraphs[0].runs[0].font.bold = Pt(18), True
    items = [i for i in deck.read_items(prs)]
    assert len(items) == 2 and items[0].set_id == items[1].set_id and items[0].align == "ctr"
    picks = [Pick(i, IconRecord("x", "x.svg", "outline", "c", []), "", []) for i in items]
    assert placement.place(picks, tier_settings(), prs) == []
    for p, panel_x in zip(sorted(picks, key=lambda p: p.item.left), (5.5, 9.0)):
        assert p.position == "beside"                                   # next to the heading, not floating above
        assert panel_x <= p.left and p.top >= 1.5                        # on the same panel as its text
        assert p.item.tx_left - (p.left + p.size) < 0.25                 # right by the text, not by the box edge


def test_shapes_hidden_under_the_texts_surface_dont_block_the_icon():
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from app import placement
    prs, _ = boxes_slide([])
    slide = prs.slides[0]
    under = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1.0), Inches(1.0), Inches(3.0), Inches(3.0))
    under.fill.solid()
    under.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_2           # hidden under the panel where the icon goes
    panel = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(2.0), Inches(1.5), Inches(5.0), Inches(5.0))
    panel.fill.solid()
    panel.fill.fore_color.theme_color = MSO_THEME_COLOR.BACKGROUND_2
    head = slide.shapes.add_textbox(Inches(3.5), Inches(3.0), Inches(2.6), Inches(0.5))   # well inside the oval
    head.text_frame.text = "Enabling technologies"
    picks = card_picks(prs, [head.shape_id], set_id=None)
    assert placement.place(picks, tier_settings(), prs) == [] and picks[0].position == "beside"


def test_a_set_that_cant_be_placed_releases_its_icons_for_the_next_set(tmp_path):
    lib = make_library(tmp_path / "lib")
    prs = diagram_slide("HEXAGON", [(1, 1, 1.2, 0.35, "Team"), (3, 1, 1.2, 0.35, "Revenue growth")], font=12)
    slide2 = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((2, "Talent pipeline"), (7, "Revenue growth")):            # headings on the next slide
        slide2.shapes.add_textbox(Inches(x), Inches(4), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(tmp_path / "d.pptx")
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")
    # slide 1's nodes are too small for icons, so that slide gets none; their claim on people-team (as "team")
    # is released, so slide 2's heading can use it for "talent"
    assert {(p.item.text, p.icon.id) for p in r.picks} == {("Talent pipeline", "people-team"),
                                                          ("Revenue growth", "finance-growth-chart")}
    assert any(f.reason.startswith("no room") and f.item.rank == 0 for f in r.flags)


def test_when_the_main_component_cant_take_icons_its_headings_may_but_never_bullets(tmp_path):
    lib = make_library(tmp_path / "lib")
    prs = diagram_slide("HEXAGON", [(1, 1, 1.2, 0.35, "Team"), (3, 1, 1.2, 0.35, "Revenue growth")], font=12)
    for x, text in ((2, "Talent pipeline"), (7, "Revenue growth")):            # lesser headings, same slide
        prs.slides[0].shapes.add_textbox(Inches(x), Inches(4), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(tmp_path / "d.pptx")
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")
    assert {p.item.text for p in r.picks} == {"Talent pipeline", "Revenue growth"} and all(p.item.rank == 1 for p in r.picks)
    assert all(f.reason.startswith("no room") for f in r.flags if f.item.rank == 0)   # the nodes are too small

    prs = diagram_slide("HEXAGON", [(1, 1, 1.2, 0.35, "Team"), (3, 1, 1.2, 0.35, "Revenue growth")], font=12)
    tb = prs.slides[0].shapes.add_textbox(Inches(2), Inches(4), Inches(6), Inches(1.2))
    tb.text_frame.text = "Talent pipeline\nRisk management"
    for para in tb.text_frame.paragraphs:                                         # a bullet list instead
        ppr = para._p.get_or_add_pPr()
        ppr.append(ppr.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}buChar", {"char": "•"}))
    prs.save(tmp_path / "d2.pptx")
    r = pipeline.run(tmp_path / "d2.pptx", lib, "suggest")
    assert r.picks == []
    assert {f.reason.split(" (")[0] for f in r.flags if f.item.rank == 3} == {"only the slide's main component gets icons"}


def test_icons_match_their_labels_when_the_designer_colored_them_per_background():
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from app import colors, deck, placement
    from app.models import IconRecord, Pick
    prs, _ = boxes_slide([])
    slide = prs.slides[0]
    for x, panel_color, text_color in ((1.0, MSO_THEME_COLOR.TEXT_2, MSO_THEME_COLOR.BACKGROUND_1),
                                       (6.0, MSO_THEME_COLOR.BACKGROUND_2, MSO_THEME_COLOR.TEXT_1)):
        panel = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(1.5), Inches(4.5), Inches(4.5))
        panel.fill.solid()
        panel.fill.fore_color.theme_color = panel_color
        head = slide.shapes.add_textbox(Inches(x + 1.0), Inches(2.0), Inches(3.0), Inches(0.5))
        head.text_frame.text = "Revenue growth" if x < 5 else "Risk management"
        head.text_frame.paragraphs[0].runs[0].font.color.theme_color = text_color
    items = deck.read_items(prs)
    picks = [Pick(i, IconRecord("x", "x.svg", "outline", "c", []), "", []) for i in items]
    assert len(picks) == 2 and placement.place(picks, tier_settings(), prs) == []
    colors.assign(prs, picks, "accent1")
    assert {(p.item.text, p.color_slot) for p in picks} == {("Revenue growth", "bg1"), ("Risk management", "tx1")}
    assert all("matches its label" in p.color_note for p in picks)


def banner_slide(path, left_inset=None):
    """Two filled banners with bold caps text and no alignment set (PowerPoint shows it left-aligned)."""
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((0.6, "REVENUE GROWTH"), (6.9, "RISK MANAGEMENT")):
        b = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(1.5), Inches(5.8), Inches(0.5))
        b.fill.solid()
        b.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_2
        b.text_frame.text = text
        b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        if left_inset is not None:
            b.text_frame.margin_left = Inches(left_inset)
        para = b.text_frame.paragraphs[0]
        para._p.get_or_add_pPr().attrib.pop("algn", None)
        para.runs[0].font.size, para.runs[0].font.bold = Pt(14), True
    prs.save(path)


def test_banner_text_at_its_edge_moves_over_to_make_room_for_the_icon(tmp_path):
    lib = make_library(tmp_path / "lib")
    banner_slide(tmp_path / "banners.pptx")
    r = pipeline.run(tmp_path / "banners.pptx", lib, "suggest")
    assert len(r.picks) == 2 and all(p.item.align == "l" for p in r.picks)       # unset alignment reads as left
    for p in r.picks:
        assert p.position == "beside" and p.indent > 0
        assert p.left >= p.item.left and p.left + p.size + 0.05 <= p.item.tx_left + p.indent   # clear of the text
    saved = json.loads(json.dumps(report.to_dict(r)))
    out, _ = pipeline.insert(tmp_path / "banners.pptx", lib, pipeline.placements_from_report(saved, [0, 1]),
                             saved["settings"])
    banners = sorted((s for s in Presentation(out).slides[0].shapes if s.has_text_frame and s.text_frame.text),
                     key=lambda s: s.left)
    for shape, p in zip(banners, sorted(r.picks, key=lambda p: p.item.left)):
        assert abs(shape.text_frame.margin_left - Inches(0.1 + p.indent)) < Inches(0.01)   # the text moved over


def test_room_the_designer_left_before_the_text_is_used_as_is(tmp_path):
    lib = make_library(tmp_path / "lib")
    banner_slide(tmp_path / "banners.pptx", left_inset=0.6)
    r = pipeline.run(tmp_path / "banners.pptx", lib, "suggest")
    assert len(r.picks) == 2 and all(p.position == "beside" and p.indent == 0 for p in r.picks)
    assert all(p.left >= p.item.left and p.left + p.size <= p.item.tx_left for p in r.picks)


def test_an_icon_can_mean_another_thing_on_another_slide_but_never_on_the_same_slide(tmp_path):
    lib = make_library(tmp_path / "lib")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for y, texts in ((3, ("Team expansion", "Revenue growth")), (3, ("Talent pipeline", "Risk management")),
                     (5.6, ("Team culture", "Talent pipeline"))):    # slide 3 is built differently (lower down)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        for x, text in zip((2, 7), texts):
            slide.shapes.add_textbox(Inches(x), Inches(y), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(tmp_path / "d.pptx")
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")
    by_slide = {(p.item.slide, p.item.text): p for p in r.picks}
    talent = by_slide[(2, "Talent pipeline")]                      # people-team already means "team" on slide 1
    assert talent.icon.id == "people-team" and "elsewhere in the deck" in talent.reason
    assert not any(p.item.slide == 3 for p in r.picks)             # slide 3 would need it for two meanings


def test_near_synonyms_in_a_set_fall_back_to_their_next_idea(tmp_path):
    from app.models import Item
    from app.selector import Selector
    lib = make_library(tmp_path / "lib")

    def item(slide, sid, text, concepts):
        return Item(slide=slide, shape_id=sid, text=text, kind="card", left=sid, top=0, w=1, h=1, set_id="s",
                    concept=concepts[0], concepts=concepts)
    a, b = item(1, 1, "Benchmarking", ["growth"]), item(1, 2, "Operational metrics", ["increase", "team"])
    picks, flags = Selector(lib, tier_settings()).pick_set([a, b])     # "increase" only fits the growth chart
    assert flags == [] and [(p.item.concept, p.icon.id) for p in picks] == [("growth", "finance-growth-chart"),
                                                                             ("team", "people-team")]
    assert "first idea 'increase' had no free icon on this slide" in picks[1].reason
    a, b = item(2, 1, "Benchmarking", ["growth"]), item(2, 2, "Metrics", ["increase"])
    picks, flags = Selector(lib, tier_settings()).pick_set([a, b])     # no other idea: the set is flagged
    assert picks == [] and "already used for other items on this slide" in flags[0].reason


def test_slides_with_the_same_structure_get_icons_together_or_not_at_all(tmp_path, monkeypatch):
    lib = make_library(tmp_path / "lib")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for texts in (("Team expansion", "Revenue growth"), ("Risk management", "Next steps")):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        for x, text in zip((2, 7), texts):
            slide.shapes.add_textbox(Inches(x), Inches(3), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(tmp_path / "d.pptx")
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")           # slide 2: "Next steps" has no metaphor
    assert r.picks == []
    held = [f for f in r.flags if f.item.slide == 1]
    assert held and all(f.reason.startswith("held back to keep slides with the same structure consistent (2)")
                        for f in held)
    monkeypatch.setattr(config, "SAME_STRUCTURE_ALL_OR_NONE", False)
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")
    assert {p.item.slide for p in r.picks} == {1}


def test_same_structure_slides_share_one_placement():
    from app import placement
    from app.models import IconRecord, Item, Pick
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    sets = []
    for n, left in ((1, 0.5), (2, 2.5)):         # slide 1: no room beside (slide edge), so both go above
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        tb = slide.shapes.add_textbox(Inches(left), Inches(3), Inches(3), Inches(0.6))
        tb.text_frame.text = "Revenue growth"
        it = Item(slide=n, shape_id=tb.shape_id, text="Revenue growth", kind="card", left=left, top=3, w=3, h=0.6)
        sets.append([Pick(it, IconRecord("x", "x.svg", "outline", "c", []), "", [])])
    placer = placement.Placer(tier_settings(), prs)
    assert placer.place_family(sets) == [[], []]
    assert {s[0].position for s in sets} == {"above"} and sets[0][0].size == sets[1][0].size


def holder_slide(path):
    """Three panels, each with a label at the top and an empty ring-and-disc holder at the bottom, in groups."""
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((1, "Revenue growth"), (5, "Team expansion"), (9, "Risk management")):
        group = slide.shapes.add_group_shape()
        panel = group.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(1.5), Inches(3), Inches(4.5))
        panel.fill.solid()
        panel.fill.fore_color.theme_color = MSO_THEME_COLOR.BACKGROUND_2
        panel.text_frame.text = text
        panel.text_frame.vertical_anchor = MSO_ANCHOR.TOP
        holder = group.shapes.add_group_shape()
        ring = holder.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x + 1), Inches(4.6), Inches(1), Inches(1))
        ring.fill.background()
        disc = holder.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x + 1.1), Inches(4.7), Inches(0.8), Inches(0.8))
        disc.fill.solid()
        disc.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
    prs.save(path)


def test_empty_icon_holders_get_the_icon_for_their_label(tmp_path):
    from app import deck
    lib = make_library(tmp_path / "lib")
    holder_slide(tmp_path / "h.pptx")
    prs = Presentation(tmp_path / "h.pptx")
    assert deck.existing_icons(prs) == []                                  # empty holders aren't icons
    items = [i for i in deck.read_items(prs) if i.holder]
    assert [i.text for i in items] == ["Revenue growth", "Team expansion", "Risk management"]
    assert all(i.rank == -1 and i.set_id == items[0].set_id for i in items)
    r = pipeline.run(tmp_path / "h.pptx", lib, "suggest")
    assert len(r.picks) == 3 and {p.position for p in r.picks} == {"in its holder"}
    for p, x in zip(sorted(r.picks, key=lambda p: p.left), (1, 5, 9)):
        assert abs(p.left + p.size / 2 - (x + 1.5)) < 0.01 and abs(p.top + p.size / 2 - 5.1) < 0.01
        assert abs(p.size - 0.48) < 0.01                                   # 60% of the 0.8in disc
    assert not any(f.item.rank == -1 for f in r.flags)


def test_beside_a_centered_heading_clears_its_wider_second_line():
    from pptx.enum.text import PP_ALIGN
    from app import deck, placement
    from app.models import IconRecord, Pick
    prs, ids = boxes_slide([(3, 2, 2.6, 0.8, "Cost Optimization And Profitability Enhancement"),
                            (8, 2, 2.6, 0.8, "Turnaround Strategy")])
    for tb in prs.slides[0].shapes:
        tb.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    it = deck.read_items(prs)[0]
    assert len(it.rows) == 2 and it.rows[1][0] < it.rows[0][0]           # the second line starts further left
    picks = [Pick(i, IconRecord("x", "x.svg", "outline", "c", []), "", []) for i in deck.read_items(prs)]
    assert placement.place(picks, tier_settings(), prs) == [] and picks[0].position == "beside"
    assert picks[0].left + picks[0].size <= it.rows[1][0]                 # clear of both lines


FAMILY_ICONS = [  # id, family, tags, house picks, contexts
    ("growth-arrow", "simple", ["growth", "increase"], ["growth"], []),
    ("growth-chart", "detailed", ["growth", "revenue growth"], [], []),
    ("people", "simple", ["team", "people"], ["team"], []),
    ("people-ring", "detailed", ["team", "community"], [], []),
    ("shield", "simple", ["risk", "security"], ["risk"], []),
    ("shield-check", "detailed", ["risk", "compliance"], [], []),
    ("org-chart", "simple", ["organization", "structure"], ["organization"], []),
    ("merge", "detailed", ["consolidation", "centralization"], [], []),
    ("tick", "simple", ["approval", "done", "yes"], [], ["operations"]),
    ("x-mark", "simple", ["rejection", "no"], [], []),
    ("hand-wave", "simple", ["hand", "greeting"], [], []),                   # blocked by regional rule
]


def family_library(root):
    """A library with two icon families, like the brand library's simple glyphs and detailed line icons."""
    root.mkdir(parents=True)
    recs = []
    for icon_id, family, tags, house, contexts in FAMILY_ICONS:
        (root / f"{icon_id}.svg").write_text(SVG.format(d="M4 12h16"))
        recs.append({"id": icon_id, "file": f"{icon_id}.svg", "status": "approved", "style": "outline",
                     "family": family, "category": "x", "description": f"Drawing of {icon_id}", "tags": tags,
                     "preferred_for": house, "contexts": contexts})
    (root / "icons.json").write_text(json.dumps(recs))
    return Library(root)


def pick_item(slide, sid, text, pick, set_id="s"):
    """An item as Claude left it: its one pick, or "" when nothing fits."""
    from app.models import Item
    return Item(slide=slide, shape_id=sid, text=text, kind="card", left=sid, top=0, w=1, h=1, set_id=set_id,
                concept=pick or None, concepts=[pick] if pick else [], icon_pick=pick)


def test_contexts_alone_never_match_and_uk_spellings_and_plurals_resolve(tmp_path):
    lib = family_library(tmp_path / "lib")
    assert "tick" not in [i.id for i in lib.search("operations", "outline")]   # only a context, not a meaning
    assert lib.lookup("Organisation") == "organization" and lib.lookup("teams") == "team"
    assert lib.lookup("weather") is None
    from app.library import spellings
    assert spellings("business processes") == ["business processes", "business processe"]   # "-ss" kept


def test_the_prompt_gives_claude_the_library_but_no_concept_map(tmp_path):
    from app import concepts
    prompt = concepts.system_prompt(family_library(tmp_path / "lib"), "outline")
    assert "growth-arrow | simple | Drawing of growth-arrow | growth, increase\n" in prompt
    assert "house pick" not in prompt and "CONCEPT MAP" not in prompt
    assert "hand-wave" not in prompt                                     # regional block list
    assert '"simple" = bold, minimal glyphs' in prompt and "Use one family for every point" in prompt
    assert '{"choices":[{"slide":<slide number>' in prompt
    plain = concepts.system_prompt(make_library(tmp_path / "plain"), "outline")   # a library without families
    assert "style variants of the same pictogram" in plain


def test_each_set_is_one_slide_entry_and_claudes_choices_map_back(tmp_path):
    from app import concepts
    lib = family_library(tmp_path / "lib")
    items = [pick_item(4, 1, "Revenue growth\nUp 22% on last year", None, "a"),
             pick_item(4, 2, "Team expansion", None, "a"),
             pick_item(4, 3, "Our approach", None, "b"),                 # another set on the same deck slide
             pick_item(5, 1, "Risk management", None, None)]
    for i in items:
        i.icon_pick = None
    sent = []
    names = {"Revenue growth | Up 22% on last year": "growth-chart | detailed", "Team expansion": "people-ring",
             "Our approach": "", "Risk management": "shield-made-up"}

    def icon(text):
        sent.append(text)
        return names[text]
    notes = concepts._extract_llm(items, lib, "outline", client=FakeClaude(icon=icon))
    assert sent == [*names, "Risk management"]           # a headline and its gist per point; the invented name
                                                         # breaks rule 3, so that point goes back once
    assert [i.icon_pick for i in items] == ["growth-chart", "people-ring", "", ""]
    assert notes == ["Claude named 1 icons that aren't in the library (shield-made-up); those items got no icon"]
    content = concepts._describe(concepts._groups(items))
    assert "SLIDE 0 (deck slide 4)\n  0. Revenue growth" in content and "SLIDE 1 (deck slide 4)\n  0. Our" in content


def test_sets_that_break_a_rule_go_back_to_claude_once(tmp_path):
    from types import SimpleNamespace as NS
    from app import concepts
    lib = family_library(tmp_path / "lib")
    items = [pick_item(2, 1, "Interviews", None), pick_item(2, 2, "Secondary interviews", None),
             pick_item(3, 1, "Revenue growth", None, "b")]

    class Claude:
        def __init__(self):
            self.beta = self.messages = self
            self.sent = []

        def create(self, **kw):
            content = kw["messages"][0]["content"]
            self.sent.append(content)
            again = content.startswith(concepts.RETRY)   # the first answer reuses an icon, the second doesn't
            picks = {"Interviews": "people", "Secondary interviews": "x-mark" if again else "people",
                     "Revenue growth": "growth-arrow"}
            choices = [{"slide": s, "point": p, "icon": picks[t]} for s, p, t in sent_points(content)]
            return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"choices": choices}))])

    fake = Claude()
    assert concepts._extract_llm(items, lib, "outline", client=fake) == []
    assert len(fake.sent) == 2 and "Revenue growth" not in fake.sent[1]      # only the set that broke a rule
    assert 'Earlier choices: 0 = "people", 1 = "people". Broke rule 4 ("people" is used twice).' in fake.sent[1]
    assert [i.icon_pick for i in items] == ["people", "x-mark", "growth-arrow"]
    assert concepts._broken(["growth-arrow", "people-ring", ""], {"growth-arrow": "simple", "people-ring": "detailed"}) \
        == 'rule 5 (it mixes the detailed and simple families), rule 6 (some points got "" and others an icon)'


def test_an_icon_is_used_once_per_slide(tmp_path):
    from app.selector import Selector
    lib = family_library(tmp_path / "lib")
    yes1, no, yes2 = (pick_item(2, n, t, i) for n, t, i in ((1, "Yes", "tick"), (2, "No", "x-mark"),
                                                           (3, "Yes", "tick")))
    picks, flags = Selector(lib, tier_settings()).pick_set([yes1, no, yes2])
    assert picks == [] and "Claude gave it tick, which another item on this slide already has" in flags[0].reason
    a, b = pick_item(1, 1, "Revenue growth", "growth-arrow"), pick_item(1, 2, "Team", "people")
    picks, flags = Selector(lib, tier_settings()).pick_set([a, b])
    assert flags == [] and [p.icon.id for p in picks] == ["growth-arrow", "people"]
    assert picks[0].reason == "Claude's pick"


def test_a_set_never_mixes_icon_families_and_is_all_or_nothing(tmp_path):
    from app.selector import Selector
    lib = family_library(tmp_path / "lib")
    growth, team = pick_item(1, 1, "Revenue growth", "growth-arrow"), pick_item(1, 2, "Team", "people-ring")
    picks, flags = Selector(lib, tier_settings()).pick_set([growth, team])      # simple next to detailed
    assert picks == [] and all("would mix families (detailed and simple)" in f.reason for f in flags)
    risk, blank = pick_item(2, 1, "Risk", "shield"), pick_item(2, 2, "Our heritage", "")
    picks, flags = Selector(lib, tier_settings()).pick_set([risk, blank])        # one point has no icon
    assert picks == [] and all(f.reason == "set left without icons: Claude found no icon that genuinely fits "
                                           "on 'Our heritage'" for f in flags)
    picks, flags = Selector(lib, tier_settings()).pick_set([pick_item(3, 1, "Vision", ""), pick_item(3, 2, "Values", "")])
    assert picks == [] and "no icon that genuinely fits every item" in flags[0].reason


def test_the_same_label_keeps_its_icon_across_the_deck(tmp_path):
    from app.selector import Selector
    lib = family_library(tmp_path / "lib")
    sel = Selector(lib, tier_settings())
    first, _ = sel.pick_set([pick_item(1, 1, "Risk management", "shield", set_id="a")])
    again, _ = sel.pick_set([pick_item(7, 1, "Risk  management", "shield-check", set_id="b")])   # another batch
    assert first[0].icon.id == again[0].icon.id == "shield"
    assert again[0].reason == "same icon as this label on slide 1 (Claude picked shield-check here)"
    row, _ = sel.pick_set([pick_item(9, 1, "Risk management", "shield-check", set_id="c"),
                           pick_item(9, 2, "Team", "people-ring", set_id="c")])
    assert [p.icon.id for p in row] == ["shield-check", "people-ring"]    # the label's icon would mix families


def test_claudes_picks_drive_the_deck(tmp_path, monkeypatch):
    import anthropic
    lib = family_library(tmp_path / "lib")
    make_deck(tmp_path / "deck.pptx", ["Revenue growth of 22%", "Team expansion", "Risk management"])
    names = {"Revenue growth of 22%": "growth-chart", "Team expansion": "people-ring",
             "Risk management": "shield-check"}
    monkeypatch.setattr(config, "USE_LLM", True)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: FakeClaude(icon=names.get))
    r = pipeline.run(tmp_path / "deck.pptx", lib, "suggest")
    assert [p.icon.id for p in r.picks] == ["growth-chart", "people-ring", "shield-check"]
    assert all(p.reason == "Claude's pick" for p in r.picks)


# --- PL logo placeholders ---------------------------------------------------------------------------------------
P_LETTER = [(0, 0), (40, 0), (55, 12), (55, 38), (40, 50), (15, 50), (15, 100), (0, 100)]
L_LETTER = [(60, 0), (75, 0), (75, 85), (100, 85), (100, 100), (60, 100)]


def pl_logo(shapes, x, y, size, color=None, p_letter=P_LETTER, name="Group"):
    """A stand-in PL logo drawn the way a template copy is: a ring and freeform letters, grouped."""
    from pptx.enum.shapes import MSO_SHAPE
    logo = shapes.add_group_shape()
    logo.name = name
    logo.shapes.add_shape(MSO_SHAPE.DONUT, Inches(x), Inches(y), Inches(size), Inches(size))
    for points in (p_letter, L_LETTER):
        ff = logo.shapes.build_freeform(*points[0], scale=Inches(size) / 100)
        ff.add_line_segments(points[1:], close=True)
        ff.convert_to_shape(Inches(x), Inches(y))
    for part in logo.shapes if color is not None else ():
        part.fill.solid()
        part.fill.fore_color.theme_color = color
    return logo


def placeholder_library(root):
    lib_root = root / "lib"
    make_library(lib_root)
    (lib_root / "placeholders").mkdir()
    prs = Presentation()
    pl_logo(prs.slides.add_slide(prs.slide_layouts[6]).shapes, 1, 1, 1.0)
    prs.save(lib_root / "placeholders" / "pl-logo.pptx")
    return Library(lib_root)


def placeholder_deck(path, nested=True):
    """Slide 1: three headings, each with a PL logo above it (moved, resized, recolored); the third card is a
    group the designer then stretched. A look-alike logo with a different P stands by itself. Slide 2: plain
    headings, no placeholders."""
    from pptx.enum.dml import MSO_THEME_COLOR
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text, size in ((1, "Revenue growth", 0.6), (5, "Team expansion", 0.8)):
        pl_logo(slide.shapes, x + 1, 2.0, size, MSO_THEME_COLOR.ACCENT_2)
        slide.shapes.add_textbox(Inches(x), Inches(2.9), Inches(3), Inches(0.6)).text_frame.text = text
    card = slide.shapes.add_group_shape() if nested else slide.shapes
    pl_logo(card.shapes if nested else card, 9.5, 2.0, 0.6, MSO_THEME_COLOR.ACCENT_2)
    card_text = (card.shapes if nested else card).add_textbox(Inches(8.5), Inches(2.9), Inches(3), Inches(0.6))
    card_text.text_frame.text = "Risk management"
    if nested:   # the designer stretched the card: its children are drawn 1.5 times their own size
        ext = card._element.grpSpPr.find(f"{{{NS_A}}}xfrm/{{{NS_A}}}ext")
        ext.set("cx", str(int(int(ext.get("cx")) * 1.5)))
        ext.set("cy", str(int(int(ext.get("cy")) * 1.5)))
    decoy = [(0, 0), (40, 0), (55, 20), (40, 50), (15, 50), (15, 100), (0, 100)]
    pl_logo(slide.shapes, 6, 5.5, 0.6, p_letter=decoy)
    slide2 = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((2, "Talent pipeline"), (7, "Revenue growth")):
        slide2.shapes.add_textbox(Inches(x), Inches(4), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(path)


NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def test_placeholders_are_found_by_look_however_moved_resized_or_recolored(tmp_path):
    from app import deck, placeholders
    lib = placeholder_library(tmp_path)
    placeholder_deck(tmp_path / "d.pptx", nested=False)
    prs = Presentation(tmp_path / "d.pptx")
    assert len(lib.placeholders) == 1 and lib.warnings == []
    found = placeholders.find(prs.slides[0], lib.placeholders)
    assert sorted(round(s.left / 914400, 1) for s in found) == [2.0, 6.0, 9.5]   # not the look-alike at 6, 5.5
    assert placeholders.find(prs.slides[0], []) == []                            # no reference, no names: none
    assert len(deck.existing_icons(prs)) == 4 and len(deck.existing_icons(prs, lib.placeholders)) == 1  # logos aren't icons
    items = [i for i in deck.read_items(prs, refs=lib.placeholders) if i.placeholder]
    assert [i.text for i in items] == ["Revenue growth", "Team expansion", "Risk management"]
    assert all(i.rank == -2 and i.set_id == "s1-placeholders" for i in items)
    assert [i.placeholder["box"][2] for i in items] == [0.6, 0.8, 0.6]


def test_the_brand_svg_placeholder_is_found_by_what_it_draws():
    """The confirmed PL logo is an SVG picture. The reference is the copy PowerPoint rewrote on insert (absolute
    path commands, no metadata); the original file, recolored or without its provenance block, still matches it.
    Another square SVG icon doesn't."""
    import re
    from app import placeholders
    from app.icon_tools import insert_icon
    refs, warnings = placeholders.load_refs(BRAND_LIBRARY / "placeholders")
    assert len(refs) == 1 and warnings == []
    svg = (BRAND_LIBRARY / "placeholders" / "pl-logo.svg").read_bytes()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    insert_icon(prs, 1, svg, 1, 1, 0.6, "C0504D", name="Picture 1")
    insert_icon(prs, 1, re.sub(rb"<metadata>.*?</metadata>", b"", svg, flags=re.S), 3, 1, 0.8, "002528", name="Picture 2")
    insert_icon(prs, 1, (BRAND_LIBRARY / "Target.svg").read_bytes(), 5, 1, 0.6, "002528", name="Picture 3")
    assert [s.name for s in placeholders.find(slide, refs)] == ["Picture 1", "Picture 2"]


def test_a_shape_named_as_a_placeholder_counts_without_a_reference(tmp_path):
    from app import placeholders
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pl_logo(slide.shapes, 1, 1, 0.6, name="PL logo")
    star = slide.shapes.add_shape(1, Inches(4), Inches(1), Inches(0.6), Inches(0.6))
    star.name = "Icon placeholder 2"
    pl_logo(slide.shapes, 7, 1, 0.6, name="Group 7")
    assert [s.name for s in placeholders.find(slide, [])] == ["PL logo", "Icon placeholder 2"]


def test_replace_puts_each_icon_in_its_placeholders_place(tmp_path):
    from app import colors
    lib = placeholder_library(tmp_path)
    placeholder_deck(tmp_path / "d.pptx")
    before = Presentation(tmp_path / "d.pptx").slides[0]
    boxes = {s.shape_id: (s.left, s.top, s.width, s.height) for s in colors.leaves(before.shapes, groups=True)}
    r = pipeline.run(tmp_path / "d.pptx", lib, "replace")
    assert [(p.item.text, p.icon.id) for p in r.picks] == [("Revenue growth", "finance-growth-chart"),
                                                          ("Team expansion", "people-team"),
                                                          ("Risk management", "risk-shield")]
    assert {p.position for p in r.picks} == {"in its placeholder"} and r.gaps == []
    assert not any(p.item.slide == 2 for p in r.picks)              # replace mode: nothing where there's no placeholder
    p = r.picks[2]                                                  # the stretched card: its logo is 0.9in on the slide
    assert abs(p.size - 0.9) < 0.01 and all(q.color_slot == "accent2" and "placeholder's color" in q.color_note
                                            for q in r.picks)
    out = Presentation(r.output_file).slides[0]
    shapes = list(colors.leaves(out.shapes, groups=True))
    icons = {s.name: s for s in shapes if s.name.startswith("Icon - ")}
    assert len(icons) == 3 and len([s for s in shapes if s.name == "Group"]) == 1   # only the look-alike is left
    for p in r.picks:
        icon = icons[f"Icon - {p.icon.id}"]
        l, t, w, h = boxes[p.item.placeholder["id"]]
        cx, cy = icon.left + icon.width / 2, icon.top + icon.height / 2
        assert abs(cx - (l + w / 2)) < 9144 and abs(cy - (t + h / 2)) < 9144    # centered where the logo was
        assert abs(max(icon.width, icon.height) - min(w, h)) < 9144              # as big as the logo
    nested = icons["Icon - risk-shield"]
    assert nested._element.getparent().tag.endswith("grpSp")                   # still inside its card


def test_add_mode_fills_placeholders_and_finds_spots_only_on_other_slides(tmp_path):
    lib = placeholder_library(tmp_path)
    placeholder_deck(tmp_path / "d.pptx", nested=False)
    r = pipeline.run(tmp_path / "d.pptx", lib, "suggest")
    on_one = [p for p in r.picks if p.item.slide == 1]
    assert len(on_one) == 3 and all(p.item.placeholder for p in on_one)
    assert any(p.item.slide == 2 for p in r.picks)                  # no placeholders there: it finds the spots
    saved = json.loads(json.dumps(report.to_dict(r)))
    out, _ = pipeline.insert(tmp_path / "d.pptx", lib, pipeline.placements_from_report(saved, [0]), saved["settings"])
    assert len([s for s in Presentation(out).slides[0].shapes if s.name == "Group"]) == 3   # one logo replaced


def growth_and_steps_deck(path):
    """Two PL placeholders over 'Revenue growth' and 'Next steps'. 'Next steps' has no metaphor, so the set is
    flagged."""
    from pptx.enum.dml import MSO_THEME_COLOR
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, text in ((1, "Revenue growth"), (5, "Next steps")):
        pl_logo(slide.shapes, x + 1, 2.0, 0.6, MSO_THEME_COLOR.ACCENT_2)
        slide.shapes.add_textbox(Inches(x), Inches(2.9), Inches(3), Inches(0.6)).text_frame.text = text
    prs.save(path)


def test_flagged_placeholders_offer_the_closest_icons_and_take_the_one_chosen(tmp_path):
    """'Revenue growth' offers its closest icons, with the spot, color and background its icon would get; the one a
    designer chooses replaces its placeholder. 'Next steps' offers none but still takes an icon from the library."""
    from app import colors
    lib = placeholder_library(tmp_path)
    growth_and_steps_deck(tmp_path / "d.pptx")
    saved = json.loads(json.dumps(report.to_dict(pipeline.run(tmp_path / "d.pptx", lib, "replace"))))
    assert saved["picks"] == [] and [f["item"]["text"] for f in saved["flags"]] == ["Revenue growth", "Next steps"]
    growth, steps = saved["flags"]
    assert growth["options"][0] == "finance-growth-chart" and growth["spot"]["color_slot"] == "accent2"
    assert abs(growth["spot"]["size"] - 0.6) < 0.01 and growth["spot"]["bg"] == "FFFFFF"
    assert steps["options"] == [] and steps["spot"]                     # nothing near it, but a designer can search
    assert saved["slide_size"] == [13.333, 7.5]
    assert "closest icons, for a designer to choose: finance-growth-chart" in saved["markdown"]

    out, skipped = pipeline.insert(tmp_path / "d.pptx", lib, pipeline.placements_from_report(
        saved, [], {0: "finance-growth-chart"}), saved["settings"])
    shapes = list(colors.leaves(Presentation(out).slides[0].shapes, groups=True))
    icon = next(s for s in shapes if s.name == "Icon - finance-growth-chart")
    assert skipped == [] and abs(icon.left / 914400 - 2.0) < 0.01 and abs(icon.width / 914400 - 0.6) < 0.01
    assert len([s for s in shapes if s.name == "Group"]) == 1              # 'Next steps' keeps its placeholder


def test_review_screen_shows_flagged_slides_and_adds_the_icons_chosen(tmp_path, monkeypatch):
    """Slide pictures come from the copy with icons, rendered once per run and kept until icons are added. A
    designer may choose any approved icon in the deck's style, not only the options. Previews get recolored icons."""
    from fastapi.testclient import TestClient
    from app import main, render
    monkeypatch.setattr(main, "library", placeholder_library(tmp_path))
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "jobs")
    rendered = []

    def fake_slides(deck, numbers, out_dir, aspect, width=render.WIDTH_PX):
        rendered.append((Path(deck).name, numbers, round(aspect, 2)))
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in numbers:
            (out_dir / f"slide-{n}.png").write_bytes(b"png")
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "slides", fake_slides)
    growth_and_steps_deck(tmp_path / "d.pptx")
    client = TestClient(main.app)
    with open(tmp_path / "d.pptx", "rb") as f:
        job = client.post("/jobs", files={"deck": ("d.pptx", f)}, data={"mode": "replace"}).json()["job"]
    shots = [client.get(f"/jobs/{job}/slides/1.png") for _ in range(2)]
    assert [s.content for s in shots] == [b"png", b"png"] and rendered == [("d_icons.pptx", [1], 1.78)]   # once
    assert client.get(f"/jobs/{job}/slides/2.png").status_code == 404           # no flagged placeholder there
    assert client.post(f"/jobs/{job}/apply", json={"choices": {"1": "no-such-icon"}}).status_code == 400
    added = client.post(f"/jobs/{job}/apply", json={"choices": {"1": "people-team"}})   # found by search
    assert added.status_code == 200 and added.json()["added"] == 1
    client.get(f"/jobs/{job}/slides/1.png")
    assert len(rendered) == 2                                                    # the new copy, rendered afresh
    assert 'stroke="#C0504D"' in client.get("/icons/people-team.svg?color=C0504D").text   # outline icons: stroked


def test_a_designer_can_upload_an_icon_for_a_placeholder(tmp_path, monkeypatch):
    """An upload gets the library's checks, stays with the run (the library doesn't change), previews recolored,
    and replaces the placeholder it was chosen for."""
    from fastapi.testclient import TestClient
    from app import colors, main
    lib = placeholder_library(tmp_path)
    monkeypatch.setattr(main, "library", lib)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "jobs")
    growth_and_steps_deck(tmp_path / "d.pptx")
    client = TestClient(main.app)
    with open(tmp_path / "d.pptx", "rb") as f:
        job = client.post("/jobs", files={"deck": ("d.pptx", f)}, data={"mode": "replace"}).json()["job"]
    unsafe = SVG.format(d="M4 4h16").replace("<path", '<path onclick="alert(1)"')
    refused = client.post(f"/jobs/{job}/uploads", files={"file": ("bad.svg", unsafe.encode())})
    assert refused.status_code == 400 and "script attribute" in refused.json()["detail"][0]
    up = client.post(f"/jobs/{job}/uploads", files={"file": ("steps.svg", SVG.format(d="M4 12h16M14 6l6 6-6 6").encode())})
    upload = up.json()
    assert up.status_code == 200 and upload["id"].startswith("upload:") and upload["name"] == "steps.svg"
    assert 'stroke="#C0504D"' in client.get(f"/jobs/{job}/uploads/{upload['id'][7:]}.svg?color=C0504D").text
    assert client.post(f"/jobs/{job}/apply", json={"choices": {"1": "upload:000000000000"}}).status_code == 400
    added = client.post(f"/jobs/{job}/apply", json={"choices": {"1": upload["id"]}})
    assert added.status_code == 200 and added.json()["added"] == 1
    shapes = list(colors.leaves(Presentation(next((tmp_path / "jobs" / job).glob("*_icons.pptx"))).slides[0].shapes,
                                groups=True))
    icon = next(s for s in shapes if s.name == f"Icon - {upload['id']}")
    assert abs(icon.left / 914400 - 6.0) < 0.01                                 # where the 'Next steps' logo was
    assert upload["id"] not in Library(tmp_path / "lib").all                     # the library is unchanged


def test_claude_ranks_the_closest_icons_for_flagged_placeholders(tmp_path, monkeypatch):
    """Names not in the library and blocked icons are dropped; a point left with nothing, or a failed request,
    gets keyword matches instead. The request reuses the picks' cached library prompt."""
    import anthropic
    import httpx2
    from app import concepts
    from app.models import Item
    monkeypatch.setattr(config, "USE_LLM", True)
    lib = make_library(tmp_path / "lib")
    items = [Item(slide=1, shape_id=k, text=t, kind="card", left=0, top=0, w=1, h=1)
             for k, t in enumerate(["Next steps", "Revenue growth"])]
    near = {"Next steps": ["people-team", "made-up", "partnership-handshake", "risk-shield"], "Revenue growth": []}
    fake = FakeClaude(options=lambda text: near[text])
    found, notes = concepts.closest(items, lib, "outline", client=fake)
    assert found == [["people-team", "risk-shield"], ["finance-growth-chart"]] and notes == []
    system = fake.systems[0]
    assert system[0]["text"] == concepts.system_prompt(lib, "outline") and "cache_control" in system[0]
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    found, notes = concepts.closest(items, lib, "outline", client=FakeClaude(
        fail=anthropic.APIConnectionError(request=request)))
    assert found == [[], ["finance-growth-chart"]] and "2 flagged placeholders" in notes[0]


def test_each_placeholder_gets_its_own_label_closest_pairs_first(tmp_path):
    from app import deck
    lib = placeholder_library(tmp_path)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pl_logo(slide.shapes, 2, 1.0, 0.6)                              # right on top of "Revenue growth"
    slide.shapes.add_textbox(Inches(1), Inches(1.6), Inches(3), Inches(0.4)).text_frame.text = "Revenue growth"
    pl_logo(slide.shapes, 2, 2.05, 0.6)                             # nearer that heading than to its own
    slide.shapes.add_textbox(Inches(1), Inches(2.85), Inches(3), Inches(0.4)).text_frame.text = "Team expansion"
    items = [i for i in deck.read_items(prs, refs=lib.placeholders) if i.placeholder]
    assert [(i.text, i.placeholder["box"][1]) for i in items] == [("Revenue growth", 1.0), ("Team expansion", 2.05)]


def test_a_kpi_tiles_placeholder_stands_for_its_description_not_its_value(tmp_path):
    from pptx.enum.shapes import MSO_SHAPE
    from app import deck
    lib = placeholder_library(tmp_path)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for x, value, text in ((1, "15-30%", "Reduction in operating costs"), (5, "AED 2.4bn", "Revenue growth")):
        slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(1.5), Inches(3), Inches(3))   # the tile
        pl_logo(slide.shapes, x + 1.2, 1.7, 0.6)
        slide.shapes.add_textbox(Inches(x + 0.2), Inches(2.4), Inches(2.6), Inches(0.8)).text_frame.text = value
        slide.shapes.add_textbox(Inches(x + 0.2), Inches(3.3), Inches(2.6), Inches(0.6)).text_frame.text = text
    items = [i for i in deck.read_items(prs, refs=lib.placeholders) if i.placeholder]
    assert [i.text for i in items] == ["Reduction in operating costs", "Revenue growth"]


def test_a_placeholder_without_its_own_label_leaves_the_slide_as_it_is(tmp_path):
    lib = placeholder_library(tmp_path)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pl_logo(slide.shapes, 2, 2.0, 0.6)
    slide.shapes.add_textbox(Inches(1), Inches(2.8), Inches(3), Inches(0.6)).text_frame.text = "Revenue growth"
    pl_logo(slide.shapes, 9, 5.5, 0.6)                              # nothing near it
    prs.save(tmp_path / "d.pptx")
    r = pipeline.run(tmp_path / "d.pptx", lib, "replace")
    assert r.picks == [] and r.gaps == ["Slide 1: a PL placeholder has no text beside it or on its panel, so this "
                                        "slide's placeholders were left as they are"]


# --- which text a placeholder stands for, on layouts from a real deck -------------------------------------------

HEADS = ["PROVEN TRACK RECORD", "DEEP SHARED SERVICES", "PROPRIETARY ACCELERATORS", "DEEP EXPERTISE IN PAYMENTS"]


def placeholder_labels(draw) -> list[str]:
    """Each PL placeholder's label (its first line), top to bottom and left to right."""
    from app import deck
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    draw(prs.slides.add_slide(prs.slide_layouts[6]).shapes)
    found = sorted((i for i in deck.read_items(prs) if i.placeholder),
                   key=lambda i: (round(i.placeholder["box"][1], 1), i.placeholder["box"][0]))
    return [i.text.splitlines()[0] for i in found]


def text(shapes, x, y, w, h, *lines):
    box = shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.text_frame.text = lines[0]
    for line in lines[1:]:
        box.text_frame.add_paragraph().text = line
    return box


def cards_under_a_subtitle(nearer):
    """Four cards with a PL placeholder above each, and the slide's subtitle across the row: it sits about as close
    to the icons as the cards' own headings, a little nearer to placeholder `nearer`."""
    def draw(shapes):
        text(shapes, 0.48, 1.40, 12.37, 0.35, "What We Bring To This Project")
        for k, head in enumerate(HEADS):
            pl_logo(shapes, 1.63 + 3.13 * k, 2.15 - (0.03 if k == nearer else 0), 0.65, name="PL logo")
            text(shapes, 0.48 + 3.14 * k, 3.17, 2.94, 3.29, head, "What the card says about it, in a sentence.")
    return draw


def cards_drawn_as_text_boxes(shapes):
    """Each card one tall text box with its heading at the top and its placeholder lower down inside it."""
    for k, head in enumerate(HEADS):
        text(shapes, 3.78 + 1.08 * k, 3.53, 0.95, 1.80, head)
        pl_logo(shapes, 4.17 + 1.08 * k, 4.73, 0.34, name="PL logo")


def rows_on_one_band(shapes):
    """A colored band down the left with five row labels, a placeholder at the end of each, the rows' detail to the
    right (the top row's a little nearer than its label)."""
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.enum.shapes import MSO_SHAPE
    band = shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.45), Inches(1.75), Inches(3.25), Inches(5.4))
    band.fill.solid()
    band.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_2
    for k, head in enumerate(["FINANCIAL OPERATIONS", "FINANCE AND ACCOUNTING", "HR", "IT", "PROCUREMENT"]):
        text(shapes, 0.5, 2.0 + 0.95 * k, 2.3, 0.6, head)
        pl_logo(shapes, 2.95, 2.0 + 0.95 * k, 0.65, name="PL logo")
    text(shapes, 4.05, 1.9, 3.3, 0.9, "Transaction Processing: authorization, clearing")


def label_above_and_a_figure_below(shapes):
    text(shapes, 3.89, 3.60, 0.69, 0.61, "Traditional spend and vendor management")
    pl_logo(shapes, 4.09, 4.72, 0.31, name="PL logo")
    text(shapes, 3.78, 5.48, 0.96, 0.18, "2% - 4% savings")


@pytest.mark.parametrize("draw, expected", [
    (cards_under_a_subtitle(0), HEADS),
    (cards_under_a_subtitle(3), HEADS),     # the same slide again, as a highlight copy: the same labels
    (cards_drawn_as_text_boxes, HEADS),
    (rows_on_one_band, ["FINANCIAL OPERATIONS", "FINANCE AND ACCOUNTING", "HR", "IT", "PROCUREMENT"]),
    (label_above_and_a_figure_below, ["Traditional spend and vendor management"]),
], ids=["subtitle-nearer-the-first", "subtitle-nearer-the-last", "cards-as-text-boxes", "rows-on-a-band",
        "figure-below"])
def test_each_placeholder_stands_for_its_own_text(draw, expected):
    """A text over the whole set (a subtitle across the row, the band under every row) heads the set and labels
    none of its placeholders; a card drawn as a text box labels the placeholder inside it; a figure with a word
    ("2% - 4% savings") labels one only when nothing else is near."""
    assert placeholder_labels(draw) == expected


def test_icons_claude_isnt_sure_of_are_left_for_a_designer(tmp_path, monkeypatch):
    """Claude rates each pick's fit (1-5); below ICON_MIN_FIT (3: a reasonable metaphor) the set gets no icon, and
    the flag says why and offers Claude's pick first."""
    from app import concepts, selector
    lib = make_library(tmp_path / "lib")
    picks = {"Revenue growth": "finance-growth-chart", "Team expansion": "people-team", "Risk management": "risk-shield"}
    sure = {"Revenue growth": 5, "Team expansion": 2, "Risk management": 3}
    items = [pick_item(1, k, t, None, "row") for k, t in enumerate(picks)] + [pick_item(2, 9, "Risk management", None)]
    for i in items:
        i.icon_pick = None
    fake = FakeClaude(icon=lambda t: picks[t], fit=lambda t: sure[t])
    notes = concepts._extract_llm(items, lib, "outline", client=fake)
    assert notes == ["Claude wasn't sure enough of 1 icons (fit below 3 of 5, ICON_MIN_FIT): their sets were left "
                     "for a designer, Claude's pick offered first"]
    assert [i.icon_pick for i in items] == ["finance-growth-chart", "", "risk-shield", "risk-shield"]
    assert items[1].unsure == ("people-team", 2)
    got, flags = selector.Selector(lib, tier_settings()).pick_all(items)
    assert [p.icon.id for p in got] == ["risk-shield"]                  # slide 2's set; slide 1's waits as a whole
    assert len(flags) == 3 and "wasn't sure enough: its best icon, people-team, fits 2 of 5" in flags[1].reason
    assert flags[1].options == ["people-team"]

    monkeypatch.setattr(config, "MIN_FIT", 2)                          # a lower bar takes it
    items = [pick_item(1, k, t, None, "row") for k, t in enumerate(picks)]
    concepts._extract_llm(items, lib, "outline", client=FakeClaude(icon=lambda t: picks[t], fit=lambda t: sure[t]))
    assert all(i.icon_pick for i in items)
    assert '"fit":<1 to 5>' in concepts.system_prompt(lib, "outline")
