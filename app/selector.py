"""Steps 3-4: shortlist three, then pick by the selection rules. Each item comes with up to three concepts,
best first; a set is assigned together, so two items whose first ideas need the same icon on one slide fall back
to another idea instead of leaving the whole set without icons."""
from collections import defaultdict

from . import config
from .models import DeckSettings, Flag, IconRecord, Item, Pick


class Selector:
    def __init__(self, library, settings: DeckSettings):
        self.lib = library
        self.settings = settings
        self.ledger: dict[str, str] = {}      # concept -> icon id, deck-wide (Consistency)
        self.used_by: dict[str, str] = {}     # icon id -> the concept it first stood for in this deck
        self.on_slide: dict[tuple, str] = {}  # (slide, icon id) -> concept: one icon never means two things on a slide

    def seed_ledger(self, existing: list[dict], concept_of_icon: dict[str, str] | None = None):
        """Existing icons placed earlier keep their concept. TODO: recover concepts for unnamed icons."""
        for e in existing:
            if e["icon_id"] and concept_of_icon and e["icon_id"] in concept_of_icon:
                self._remember(concept_of_icon[e["icon_id"]], e["icon_id"], e["slide"])

    def _remember(self, concept, icon_id, slide=None):
        self.ledger.setdefault(concept, icon_id)
        self.used_by.setdefault(icon_id, concept)
        self.on_slide[(slide, icon_id)] = concept

    # --- rules ---------------------------------------------------------------
    def _allowed(self, icon: IconRecord, item: Item, concept: str | None = None) -> bool:
        concept = concept or item.concept
        if icon.style != self.settings.style:
            return False
        haystack = {icon.id.lower(), *[t.lower() for t in icon.tags]}
        if any(b in h for b in config.REGIONAL_BLOCKLIST for h in haystack):
            return False
        text = item.text.lower()
        if any(a.lower() in text or a.lower() == (concept or "") for a in icon.avoid_for):
            return False
        owner = self.on_slide.get((item.slide, icon.id))   # style guide: never two meanings on one slide
        return owner is None or owner == concept

    def _free(self, icon: IconRecord, concept: str) -> bool:
        """Not yet standing for another concept in this deck."""
        return self.used_by.get(icon.id) in (None, concept)

    def shortlist(self, item: Item, concept: str | None = None) -> tuple[list[IconRecord], str]:
        c = concept or item.concept
        if c in self.ledger and self._allowed(self.lib.get(self.ledger[c]), item, c):
            return [self.lib.get(self.ledger[c])], "same icon as earlier use of this concept"
        cands, why = [], ""
        if c in self.lib.concept_map:
            entry = self.lib.concept_map[c]
            cands = [self.lib.get(i) for i in [entry["id"], *entry.get("alts", [])] if self.lib.get(i)]
            why = f"concept map: {c}"
        cands += [i for i in self.lib.search(c, self.settings.style, arabic=item.rtl) if i not in cands]
        if not why:
            why = f"tag match: {c}"
        allowed = [i for i in cands if self._allowed(i, item, c)]
        # Across the deck, an icon already standing for another concept is used only when nothing else fits
        # (style guide, Consistency: avoid reuse where the library offers a distinct fitting icon).
        ranked = [i for i in allowed if self._free(i, c)] + [i for i in allowed if not self._free(i, c)]
        if ranked and not self._free(ranked[0], c):
            why += f"; also stands for '{self.used_by[ranked[0].id]}' elsewhere in the deck (no other fitting icon)"
        return ranked[:3], why

    def pick(self, item: Item) -> Pick | Flag:
        picks, flags = self.pick_set([item])
        return (picks or flags)[0]

    def _choices(self, item: Item) -> list[tuple]:
        """(cost, concept, icon, why, alternatives) for each of the item's concepts and its shortlisted icons,
        cheapest first: an earlier concept, then the house pick, then an icon not standing for something else."""
        out = []
        for k, c in enumerate(dict.fromkeys(item.concepts or [item.concept])):
            if not c:
                continue
            short, why = self.shortlist(item, c)
            for rank, icon in enumerate(short):
                cost = 10 * k + rank + (0 if self._free(icon, c) else 3)
                out.append((cost, c, icon, why if rank == 0 else f"{why}; next best on the shortlist",
                            [i.id for i in short if i is not icon]))
        return sorted(out, key=lambda o: o[0])

    @staticmethod
    def _fits(option, chosen) -> bool:
        """One icon means one concept, and one concept one icon, among items on one slide."""
        return all((option[2].id == c[2].id) == (option[1] == c[1]) for c in chosen)

    @classmethod
    def _assign(cls, choices: list[list[tuple]]) -> list[tuple] | None:
        """The cheapest choice per item that _fits the others. Branch and bound, capped (keeps the best found)."""
        best, budget = [None, float("inf")], [20000]
        floor = [min((o[0] for o in opts), default=0) for opts in choices]
        rest = [sum(floor[k:]) for k in range(len(choices) + 1)]

        def go(k, chosen, cost):
            budget[0] -= 1
            if budget[0] < 0 or cost + rest[k] >= best[1]:
                return
            if k == len(choices):
                best[:] = [list(chosen), cost]
                return
            for o in choices[k]:
                if cls._fits(o, chosen):
                    chosen.append(o)
                    go(k + 1, chosen, cost + o[0])
                    chosen.pop()

        go(0, [], 0)
        return best[0]

    def _why_not(self, members: list[Item], choices: list[list[tuple]]) -> tuple[Item, str]:
        """The item that blocks the set, and why: no metaphor, no icon, or its icons taken by the others."""
        for m, opts in zip(members, choices):
            if not m.concept:
                return m, "no clear metaphor"
            if not opts:
                ideas = "', '".join(dict.fromkeys(m.concepts or [m.concept]))
                return m, f"nothing suitable in the library for '{ideas}'"
        chosen = []
        for m, opts in zip(members, choices):   # first come, first served: who takes the icons it needs
            fit = [o for o in opts if self._fits(o, chosen)]
            if not fit:
                takers = {c[2].id: c[1] for c in chosen if any(o[2].id == c[2].id for o in opts)}
                taken = ", ".join(f"{i} (used for '{c}')" for i, c in takers.items())
                ideas = "', '".join(dict.fromkeys(m.concepts or [m.concept]))
                return m, f"'{ideas}' only fit icons already used for other items on this slide: {taken}"
            chosen.append(fit[0])
        return members[0], "no combination of icons fits every item"

    def snapshot(self):
        return dict(self.ledger), dict(self.used_by), dict(self.on_slide)

    def restore(self, snapshot):
        """Release the picks made since the snapshot (their set was not placed after all)."""
        self.ledger, self.used_by, self.on_slide = dict(snapshot[0]), dict(snapshot[1]), dict(snapshot[2])

    def pick_set(self, members: list[Item]) -> tuple[list[Pick], list[Flag]]:
        """Restraint: a set gets icons on every member or none. Each member gets an icon for one of its
        concepts, chosen together so no icon means two things on the slide."""
        choices = [self._choices(m) if m.concept else [] for m in members]
        best = self._assign(choices) if all(choices) else None
        if best is None:
            culprit, reason = self._why_not(members, choices)
            if len(members) == 1:
                return [], [Flag(culprit, reason, [o[2].id for o in choices[0][:3]])]
            where = f" on '{' '.join(culprit.text.split())[:40]}'"
            return [], [Flag(m, f"set left without icons: {reason}{where}", [o[2].id for o in opts[:3]])
                        for m, opts in zip(members, choices)]
        had_icon = [bool(self.shortlist(m, m.concept)[0]) for m in members]   # before this set takes any
        picks = []
        for m, had, (_, c, icon, why, alts) in zip(members, had_icon, best):
            if c != m.concept:   # its first idea had no icon, or only icons the other items needed
                first = m.concept
                why += (f"; its first idea '{first}' had no free icon on this slide" if had
                        else f"; nothing in the library for its first idea '{first}'") + f", so '{c}' was used"
                m.concept = c
            self._remember(c, icon.id, m.slide)
            picks.append(Pick(m, icon, why, alts[:2]))
        return picks, []

    def pick_all(self, items: list[Item]) -> tuple[list[Pick], list[Flag]]:
        picks, flags = [], []
        sets = defaultdict(list)
        for it in items:
            sets[it.set_id or f"solo-{id(it)}"].append(it)
        for members in sets.values():
            p, f = self.pick_set(members)
            picks, flags = picks + p, flags + f
        return picks, flags
