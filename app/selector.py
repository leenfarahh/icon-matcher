"""Steps 3-4: check and assign icons by the selection rules. With Claude, each item comes with its one pick (or ""
when nothing fits), and the rules here are a check on it: the library and regional rules, an icon once per slide,
one family per set, all or nothing. A label already given an icon elsewhere in the deck keeps that icon where the
set still holds. With keyword matching, each item comes with up to three ideas, shortlisted from the concept map
and tags, and a set is assigned together so two items that want the same icon fall back to another idea."""
import re
from collections import defaultdict

from . import config
from .models import DeckSettings, Flag, IconRecord, Item, Pick


def label(item: Item) -> str:
    """What an item says, numbers and punctuation aside: "2-4% savings" and "10-20% savings" say the same."""
    return " ".join(re.findall(r"[^\W\d_]+", item.text.lower())) or " ".join(item.text.lower().split())


def _unsure(item: Item) -> str:
    icon, fit = item.unsure
    return f"Claude wasn't sure enough: its best icon, {icon}, fits {fit:g} of 5, below the bar of {config.MIN_FIT}"


class Selector:
    def __init__(self, library, settings: DeckSettings):
        self.lib = library
        self.settings = settings
        self.ledger: dict[str, str] = {}      # concept -> icon id, deck-wide (Consistency)
        self.used_by: dict[str, str] = {}     # icon id -> the concept it first stood for in this deck
        self.on_slide: set[tuple] = set()     # (slide, icon id): an icon is used once per slide
        self.labels: dict[str, tuple] = {}    # label -> (concept, icon id, slide): the same words, the same icon

    def seed_ledger(self, existing: list[dict], concept_of_icon: dict[str, str] | None = None):
        """Existing icons placed earlier keep their concept. TODO: recover concepts for unnamed icons."""
        for e in existing:
            if e["icon_id"] and concept_of_icon and e["icon_id"] in concept_of_icon:
                self._remember(concept_of_icon[e["icon_id"]], e["icon_id"], e["slide"])

    def _remember(self, concept, icon_id, slide=None, said=None):
        self.ledger.setdefault(concept, icon_id)
        self.used_by.setdefault(icon_id, concept)
        self.on_slide.add((slide, icon_id))
        if said:
            self.labels.setdefault(said, (concept, icon_id, slide))

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
        return (item.slide, icon.id) not in self.on_slide   # style guide: an icon once per slide

    def _free(self, icon: IconRecord, concept: str) -> bool:
        """Not yet standing for another concept in this deck."""
        return self.used_by.get(icon.id) in (None, concept)

    def shortlist(self, item: Item, concept: str | None = None) -> tuple[list[IconRecord], str]:
        c = concept or item.concept
        if c in self.ledger and self._allowed(self.lib.get(self.ledger[c]), item, c):
            return [self.lib.get(self.ledger[c])], "same icon as earlier use of this concept"
        cands, why = [], ""
        key = self.lib.lookup(c)
        if key:
            entry = self.lib.concept_map[key]
            cands = [self.lib.get(i) for i in [entry["id"], *entry.get("alts", [])] if self.lib.get(i)]
            why = f"concept map: {key}"
        cands += [i for i in self.lib.search(c, self.settings.style, arabic=item.rtl) if i not in cands]
        if not why:
            why = f"tag match: {c}"
        allowed = [i for i in cands if self._allowed(i, item, c)]
        # Across the deck, an icon already standing for another concept is used only when nothing else fits
        # (style guide, Consistency: avoid reuse where the library offers a distinct fitting icon).
        ranked = [i for i in allowed if self._free(i, c)] + [i for i in allowed if not self._free(i, c)]
        if ranked and not self._free(ranked[0], c):
            why += f"; also stands for '{self.used_by[ranked[0].id]}' elsewhere in the deck (no other fitting icon)"
        return ranked[:4], why

    def pick(self, item: Item) -> Pick | Flag:
        picks, flags = self.pick_set([item])
        return (picks or flags)[0]

    def _choices(self, item: Item) -> list[tuple]:
        """(cost, concept, icon, why, alternatives, label) for each way to give the item an icon, cheapest first:
        the icon its label already has elsewhere in the deck, then Claude's pick; with keyword matching, the
        shortlist for each idea instead of a pick. An icon standing for another concept in the deck costs more."""
        said, out = label(item), []

        def add(cost, c, icon, why, alts=()):
            if icon is None or not self._allowed(icon, item, c):
                return
            cost += 0 if self._free(icon, c) else 3
            out.append((cost, c, icon, why, [a for a in alts if a != icon.id], said))

        if said in self.labels:
            c, icon_id, slide = self.labels[said]
            add(-5, c, self.lib.get(icon_id), f"same icon as this label on slide {slide}")
        if item.icon_pick is not None:               # Claude chose: its pick is the only option
            if item.icon_pick:
                add(0, item.icon_pick, self.lib.get(item.icon_pick), "Claude's pick")
        else:
            for k, c in enumerate(dict.fromkeys(item.concepts or [item.concept])):
                if not c:
                    continue
                short, why = self.shortlist(item, c)
                for rank, icon in enumerate(short):
                    add(10 * k + rank, c, icon, why if rank == 0 else f"{why}; next best on the shortlist",
                        [i.id for i in short])
        best = {}
        for o in sorted(out, key=lambda o: o[0]):    # each (concept, icon) once, at its cheapest
            best.setdefault((o[1], o[2].id), o)
        return list(best.values())

    @staticmethod
    def _fits(option, chosen) -> bool:
        """An icon is used once per slide: no two items of a set share one."""
        return all(option[2].id != c[2].id for c in chosen)

    @staticmethod
    def _in_family(choices: list[list[tuple]], family: str) -> list[list[tuple]]:
        """Each item's choices in one family (icons without a family go with any)."""
        return [[o for o in opts if o[2].family in (family, "")] for opts in choices]

    def _assign_set(self, choices: list[list[tuple]]) -> list[tuple] | None:
        """The cheapest assignment that keeps the set in one icon family (style guide, Consistency)."""
        families = sorted({o[2].family for opts in choices for o in opts} - {""})
        if not families:
            return self._assign(choices)
        found = [got for got in (self._assign(self._in_family(choices, f)) for f in families) if got]
        return min(found, key=lambda got: sum(o[0] for o in got), default=None)

    @classmethod
    def _assign(cls, choices: list[list[tuple]]) -> list[tuple] | None:
        """The cheapest choice per item that _fits the others. Branch and bound, capped (keeps the best found)."""
        if not all(choices):
            return None
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

    def _why_not(self, members: list[Item], choices: list[list[tuple]]) -> tuple[Item | None, str]:
        """The item that blocks the set (None when it is the set as a whole), and why."""
        unsure = next((m for m in members if m.unsure), None)
        if len(members) > 1 and all(m.icon_pick == "" for m in members):
            return (unsure, _unsure(unsure)) if unsure else                 (None, "Claude found no icon that genuinely fits every item (a wrong icon is worse than none)")
        for m, opts in zip(members, choices):
            if m.icon_pick == "":
                return m, _unsure(m) if m.unsure else "Claude found no icon that genuinely fits"
            if m.icon_pick and not opts:
                icon = self.lib.get(m.icon_pick)
                why = ("it is already on this slide" if (m.slide, m.icon_pick) in self.on_slide else
                       f"it is marked not for {', '.join(icon.avoid_for)}" if icon and icon.avoid_for else
                       "it breaks the style or regional rules")
                return m, f"Claude picked {m.icon_pick}, but {why}"
            if not m.concept:
                return m, "no clear metaphor"
            if not opts:
                ideas = "', '".join(dict.fromkeys(m.concepts or [m.concept]))
                return m, f"nothing suitable in the library for '{ideas}'"
        if self._assign(choices):
            families = " and ".join(sorted({o[2].family for opts in choices for o in opts if o[2].family}))
            return None, f"its icons would mix families ({families}), and a set never mixes them"
        chosen = []
        for m, opts in zip(members, choices):   # first come, first served: who takes the icons it needs
            fit = [o for o in opts if self._fits(o, chosen)]
            if not fit:
                if m.icon_pick:
                    return m, f"Claude gave it {m.icon_pick}, which another item on this slide already has"
                takers = {c[2].id: c[1] for c in chosen if any(o[2].id == c[2].id for o in opts)}
                taken = ", ".join(f"{i} (used for '{c}')" for i, c in takers.items())
                ideas = "', '".join(dict.fromkeys(m.concepts or [m.concept]))
                return m, f"'{ideas}' only fit icons already used for other items on this slide: {taken}"
            chosen.append(fit[0])
        return None, "no combination of icons fits every item"

    def snapshot(self):
        return dict(self.ledger), dict(self.used_by), set(self.on_slide), dict(self.labels)

    def restore(self, snapshot):
        """Release the picks made since the snapshot (their set was not placed after all)."""
        self.ledger, self.used_by, self.on_slide, self.labels = (type(s)(s) for s in snapshot)

    def pick_set(self, members: list[Item]) -> tuple[list[Pick], list[Flag]]:
        """Restraint: a set gets icons on every member or none, each icon once on the slide, all one family."""
        choices = [self._choices(m) if m.concept else [] for m in members]
        best = self._assign_set(choices) if all(choices) else None
        if best is None:
            culprit, reason = self._why_not(members, choices)
            if len(members) == 1:
                return [], [Flag(members[0], reason, [o[2].id for o in choices[0][:3]]
                                 or ([members[0].unsure[0]] if members[0].unsure else []))]
            where = f" on '{' '.join(culprit.text.split())[:40]}'" if culprit else ""
            return [], [Flag(m, f"set left without icons: {reason}{where}",
                             [o[2].id for o in opts[:3]] or ([m.unsure[0]] if m.unsure else []))
                        for m, opts in zip(members, choices)]
        picks = []
        for m, opts, (_, c, icon, why, alts, said) in zip(members, choices, best):
            if m.icon_pick is not None:
                if icon.id != m.icon_pick:           # its label already had another icon in the deck
                    why += f" (Claude picked {m.icon_pick} here)"
            elif c != m.concept:   # its first idea had no icon, or only icons the other items needed
                first = m.concept
                had = any(o[1] == first for o in opts)
                why += (f"; its first idea '{first}' had no free icon on this slide" if had
                        else f"; nothing in the library for its first idea '{first}'") + f", so '{c}' was used"
            m.concept = c
            self._remember(c, icon.id, m.slide, said)
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
