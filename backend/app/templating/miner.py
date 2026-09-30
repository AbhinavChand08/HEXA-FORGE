"""
Offline template mining over a corpus of flattened configs (design section 7.4).

  1. Group files into vendor profiles (folder name, the detected vendor profile, or
     similarity of their top-level commands).
  2. Learn level by level. Children are grouped by their PARENT'S TEMPLATE, so every
     'interface <IFNAME>' shares one scope; scope falls out of the path.
       a. LEXER - typed tokens, highest priority first:
            <SECRET> <CIDR> <MASK> <IPV4> <IPV6> <MAC> <TIME> <IFNAME> <NUM> <QSTR>
       b. Free text and variable-length lists collapse into one slot:
            description Uplink to ISP        -> description <TEXT>
            allowaccess ping https ssh       -> allowaccess <WORD...>
       c. DRAIN (drain3) clusters the typed skeletons of one scope, so lines that
          share a skeleton land together.
       d. Each word position in a cluster becomes a slot only with evidence:
            not command-shaped (RO, tier_1), right after a value keyword
            (hostname, community ...), one of several interchangeable blocks,
            or a line-final word that varies as a mutually exclusive choice.
          Otherwise it stays a KEYWORD and the cluster is split on it, because
          Drain alone would merge 'ip ssh version <NUM>' with 'ip ssh time-out
          <NUM>': an over-split template costs the admin one click, an
          over-merged one silently mixes security controls.
       e. Word slots with low cardinality become <ENUM> (observed values kept),
          the rest <WORD>.
  3. Template id = signature = hash(vendor_family, dialect, template_path), so it is
     stable across runs and safe to key normalisation mappings on.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from drain3.drain import Drain

from app.core.models import pick_examples
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer
from app.templating.signature import (extract_values, file_signature, overlap, render,
                                      signature)
from app.templating.token_types import enum_shaped, keyword_shaped


def jaccard(a, b) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


@dataclass(eq=False)
class Entry:
    file: str
    dialect: str
    tokens: list[str]
    roles: list[str] | None
    quoted: list[bool]
    types: list[str | None]
    line_no: int | None
    raw: str
    cmd_id: int
    parent_id: int | None
    is_block: bool
    child_sig: frozenset
    negated: bool = False
    blob: str | None = None
    els: list[tuple] = field(default_factory=list)   # typed skeleton, one (kind, value) per slot
    tid: str | None = None

    @property
    def scope(self) -> tuple:
        return (self.file, self.parent_id)


@dataclass(eq=False)
class Template:
    id: str
    profile: str
    dialect: str
    parent: str | None
    pattern: tuple
    structured: bool
    path: list[str]
    entries: list[Entry] = field(default_factory=list)


class TemplateLearner:
    def __init__(self, hints: LearnerHints):
        self._base_h = hints
        self.h = hints
        self.lexer = Lexer(hints.ifname_prefixes)
        self.value_after = {w.lower() for w in hints.value_after}
        self.text_after = {w.lower() for w in hints.text_after}
        self.list_after = {w.lower() for w in hints.list_after}
        self.reserved = {w.lower() for w in hints.reserved_keywords}
        self.by_id: dict[str, Template] = {}
        self.value_words: set[str] = set()
        self.choice_words: set[str] = set()

    # ---------------------------------------------------------------- driver
    def learn_profile(self, profile: str, files, profile_row: dict | None = None) -> None:
        """files: list of (file_name, dialect, [FlatCommand]). profile_row (design 7.2, 7.3):
        the vendor's own ifname_prefixes / value_after / text_after / list_after /
        reserved_keywords, layered over the hints this learner was built with."""
        h = LearnerHints.for_profile(profile_row, base=self._base_h)
        self.h, self.lexer = h, Lexer(h.ifname_prefixes)
        self.value_after = {w.lower() for w in h.value_after}
        self.text_after = {w.lower() for w in h.text_after}
        self.list_after = {w.lower() for w in h.list_after}
        self.reserved = {w.lower() for w in h.reserved_keywords}
        by_depth: dict[int, list[Entry]] = defaultdict(list)
        all_entries: list[Entry] = []
        for fname, dialect, cmds in files:
            kids = defaultdict(list)
            for c in cmds:
                if c.parent_id is not None:
                    kids[c.parent_id].append(c)
            for c in cmds:
                quoted = c.quoted or [False] * len(c.tokens)
                e = Entry(fname, dialect, c.tokens, c.roles, quoted,
                          self.lexer.types(c.tokens, quoted, c.roles), c.src_line, c.raw,
                          c.id, c.parent_id, c.is_block,
                          frozenset(k.tokens[0] for k in kids.get(c.id, ())),
                          negated=c.negated, blob=c.blob)
                by_depth[len(c.context)].append(e)
                all_entries.append(e)

        self.value_words, self.choice_words = self._find_value_words(all_entries)

        tid_of: dict[tuple, str | None] = {}
        for depth in sorted(by_depth):
            groups = defaultdict(list)
            for e in by_depth[depth]:
                ptid = None if e.parent_id is None else tid_of[(e.file, e.parent_id)]
                groups[(e.dialect, ptid)].append(e)
            for (dialect, ptid), group in groups.items():
                learned = self._learn_structured([e for e in group if e.roles is not None])
                for pattern, members in learned.items():
                    self._register(profile, dialect, ptid, pattern, members, True)
                learned = self._learn_cli([e for e in group if e.roles is None])
                for pattern, members in learned.items():
                    self._register(profile, dialect, ptid, pattern, members, False)
            for e in by_depth[depth]:
                tid_of[(e.file, e.cmd_id)] = e.tid

    # ---------------------------------------------------------------- structured
    def _learn_structured(self, entries) -> dict:
        """XML / JSON / YAML / INI: keys are keywords, values are typed slots."""
        shapes = defaultdict(list)
        for e in entries:
            shapes[tuple((r, t if r == "key" else None) for t, r in zip(e.tokens, e.roles))].append(e)
        out = defaultdict(list)
        for shape, members in shapes.items():
            pattern = []
            for p, (role, key) in enumerate(shape):
                if role == "key":
                    pattern.append(("kw", key))
                    continue
                prev = shape[p - 1][1].lower() if p and shape[p - 1][0] == "key" else ""
                name_like = role == "name" or prev in self.value_after or prev.endswith("name")
                pattern.append(("slot", self._value_type(
                    [e.types[p] for e in members], [e.tokens[p] for e in members], name_like)))
            out[tuple(pattern)].extend(members)
        return out

    def _value_type(self, types, words, name_like) -> str:
        kinds = set(types)
        if len(kinds) == 1 and None not in kinds:
            return kinds.pop()                              # every value has one type
        if kinds != {None}:
            return "WORD"                                   # mixed types
        distinct = set(words)
        known = len(distinct) == 1 and next(iter(distinct)).lower() in self.value_words
        if not name_like and (len(distinct) >= 2 or known):
            return self._enum_or_word(words, known)
        return "WORD"

    def _enum_or_word(self, words, known=False) -> str:
        """Low cardinality: few distinct choice words that recur across the corpus."""
        distinct = set(words)
        if (len(distinct) <= self.h.enum_max_values and all(enum_shaped(w) for w in distinct)
                and (known or len(words) > len(distinct))):
            return "ENUM"
        return "WORD"

    # ---------------------------------------------------------------- CLI
    def _learn_cli(self, entries) -> dict:
        if not entries:
            return {}
        for e in entries:
            e.els = self._skeleton(e)
        self._collapse_lists(entries)

        drain = Drain(depth=self.h.drain_depth, sim_th=self.h.drain_sim_th,
                      parametrize_numeric_tokens=False)
        clusters = defaultdict(list)
        for e in entries:
            cluster, _ = drain.add_log_message(render(e.els))
            clusters[(cluster.cluster_id, len(e.els))].append(e)

        out = defaultdict(list)
        for members in clusters.values():
            for pattern, es in self._resolve(members).items():
                out[pattern].extend(es)

        # Drain groups greedily: 'logging trap informational' may have landed next to
        # 'logging buffered informational'. Fold such leftovers into an ENUM template
        # of this scope when the word is a known value word.
        enums = [q for q in out if ("slot", "ENUM") in q]
        for pattern in list(out):
            for q in enums:
                if q != pattern and q in out and len(q) == len(pattern) and all(
                        a == b or (a[0] == "kw" and b == ("slot", "ENUM")
                                   and a[1].lower() in self.value_words)
                        for a, b in zip(pattern, q)):
                    out[q].extend(out.pop(pattern))
                    break
        return out

    def _skeleton(self, e: Entry) -> list[tuple]:
        """Typed tokens; free text and hinted lists collapse into one trailing slot.
        reserved_keywords (design 7.3) stay literal even when the lexer types them."""
        els = []
        for i, (tok, typ) in enumerate(zip(e.tokens, e.types)):
            prev = els[-1][1].lower() if els and els[-1][0] == "kw" else None
            if prev in self.text_after:
                els.append(("text", "TEXT"))
                break
            if prev in self.list_after:
                rest = set(e.types[i:])
                els.append(("list", rest.pop() if len(rest) == 1 and None not in rest else "WORD"))
                break
            if tok.lower() in self.reserved:
                els.append(("kw", tok))
            else:
                els.append(("slot", typ) if typ else ("kw", tok))
        return els

    def _collapse_lists(self, entries) -> None:
        """Variable-length tails in one scope:  service <QSTR> <QSTR> <QSTR>  ->  service <QSTR...>"""
        by_prefix = defaultdict(list)
        for e in entries:
            if e.is_block or e.els[-1][0] in ("list", "text"):
                continue
            for k in range(1, len(e.els)):
                by_prefix[tuple(e.els[:k])].append(e)
        done: dict[int, tuple] = {}
        for prefix in sorted(by_prefix, key=len):              # longest tail wins
            es = [e for e in by_prefix[prefix] if id(e) not in done]
            kind = self._list_kind([e.els[len(prefix):] for e in es]) if es else None
            if kind:
                for e in es:
                    done[id(e)] = (len(prefix), kind)
        for e in entries:
            if id(e) in done:
                k, kind = done[id(e)]
                e.els = e.els[:k] + [("list", kind)]

    def _list_kind(self, tails) -> str | None:
        lengths = {len(t) for t in tails}
        flat = [el for t in tails for el in t]

        # 1. same-type values:  <QSTR> <QSTR> <QSTR>  /  <IPV4> <IPV4> ...
        if (all(el[0] == "slot" for el in flat) and len({el[1] for el in flat}) == 1
                and (max(lengths) >= 3 or (len(lengths) > 1 and max(lengths) >= 2))):
            return flat[0][1]

        # 2. option words that build on each other (prefix-chained):
        #        allowaccess ping / ping https ssh / ping https ssh snmp
        #    command families branch instead and are NOT lists:
        #        ip source-route / ip bootp server / service pad
        if not all(el[0] == "kw" and keyword_shaped(el[1]) for el in flat):
            return None
        unique = {tuple(el[1] for el in t) for t in tails}
        if len(unique) < 3 or any(len(t) != len(set(t)) for t in unique):
            return None
        chained = sum(1 for t in unique
                      if any(o != t and (o[:len(t)] == t or t[:len(o)] == o) for o in unique))
        return "WORD" if chained / len(unique) >= self.h.list_chain_ratio else None

    def _resolve(self, members) -> dict:
        """One Drain cluster -> templates. A word position without value evidence splits
        it (leftmost first: command words come before their values)."""
        n = len(members[0].els)
        slots = {}
        for p in range(n):
            col = {e.els[p] for e in members}
            if all(el[0] == "kw" for el in col):
                kind = self._word_slot(members, p)
                if kind:
                    slots[p] = ("slot", kind)
                    continue
            if len(col) > 1:
                parts = defaultdict(list)
                for e in members:
                    parts[e.els[p]].append(e)
                out = defaultdict(list)
                for part in parts.values():
                    for pattern, es in self._resolve(part).items():
                        out[pattern].extend(es)
                return out
        return {tuple(slots.get(p, el) for p, el in enumerate(members[0].els)): members}

    def _word_slot(self, members, p) -> str | None:
        """Is the plain word at position p a value? -> 'WORD' / 'ENUM' / None (keyword)."""
        distinct = {e.els[p][1] for e in members}
        if distinct & self.reserved:
            return None
        words = [e.els[p][1] for e in members]
        prev = {e.els[p - 1] for e in members} if p else set()
        if len(prev) == 1 and next(iter(prev))[0] == "kw" and \
                next(iter(prev))[1].lower() in self.value_after:
            return "WORD"                                   # hostname X, community X
        if distinct & self._instance_words(members, p):
            return "WORD"                                   # one of several similar blocks
        if not all(keyword_shaped(w) for w in distinct):
            return self._enum_or_word(words) if len(distinct) >= 2 else "WORD"
        final = p == len(members[0].els) - 1 and not any(e.is_block for e in members)
        if final and all(w.lower() in self.choice_words for w in distinct):
            return "ENUM"                                   # strong-crypto enable|disable
        if len(distinct) < 2 or not final:
            return None
        if all(w.lower() in self.value_words for w in distinct) or self._exclusive(members, p):
            return self._enum_or_word(words, known=True)
        return None

    def _exclusive(self, members, p) -> bool:
        """Words never seen together in one scope: alternatives of one setting."""
        by_scope = defaultdict(set)
        for e in members:
            by_scope[e.scope].add(e.els[p][1])
        return (len(by_scope) >= self.h.enum_min_scopes
                and all(len(ws) == 1 for ws in by_scope.values()))

    def _instance_words(self, members, p) -> set[str]:
        """Lowercase names of interchangeable sibling blocks (3+ with similar children)."""
        by_unit = defaultdict(lambda: defaultdict(list))
        for e in members:
            by_unit[e.scope][e.els[p][1]].append(e)
        found = set()
        for wmap in by_unit.values():
            blocks = {w: frozenset().union(*(e.child_sig for e in es))
                      for w, es in wmap.items()
                      if keyword_shaped(w) and all(e.is_block for e in es)}
            # tables whose children are all one key ('edit ...') are containers, not instances
            blocks = {w: s for w, s in blocks.items() if len(s) >= 2}
            if len(blocks) < 3:
                continue
            for w, sig in blocks.items():
                similar = sum(1 for v, s in blocks.items()
                              if v != w and jaccard(sig, s) >= self.h.block_similarity)
                if similar >= 2:
                    found.add(w)
        return found

    def _find_value_words(self, entries) -> tuple[set[str], set[str]]:
        """value words end lines after many different keys (enable, disable, Enable ...);
        choice words are value words seen as alternatives after one key (enable|disable)."""
        keys = defaultdict(set)
        follow = defaultdict(set)
        for e in entries:
            if e.is_block or len(e.tokens) < 2 or e.types[-1] is not None:
                continue
            if e.roles is not None and (e.roles[-1] != "value" or e.roles[-2] != "key"):
                continue
            w, k = e.tokens[-1], e.tokens[-2].lower()
            if keyword_shaped(w) if e.roles is None else enum_shaped(w):
                keys[w.lower()].add(k)
                follow[k].add(w.lower())
        value = {w for w, k in keys.items() if len(k) >= self.h.value_word_min_keys}
        alt_keys = defaultdict(set)
        for k, ws in follow.items():
            if len(ws & value) >= 2:
                for w in ws & value:
                    alt_keys[w].add(k)
        return value, {w for w, k in alt_keys.items() if len(k) >= 2}

    def _register(self, profile, dialect, ptid, pattern, entries, structured) -> None:
        path = (self.by_id[ptid].path if ptid else []) + [render(pattern)]
        sig = signature(profile, dialect, path)
        t = self.by_id.get(sig)
        if t is None:
            t = Template(sig, profile, dialect, ptid, tuple(pattern), structured, path)
            self.by_id[sig] = t
        t.entries.extend(entries)
        for e in entries:
            e.tid = sig

    # ---------------------------------------------------------------- output
    def to_json(self, profiles) -> dict:
        out = {"version": 3, "profiles": {}, "templates": []}
        for name, p in profiles.items():
            out["profiles"][name] = {
                "dialect": p["dialect"],
                "files": [str(f) for f, _, _ in p["members"]],
                "signature": sorted(set().union(*p["sigs"])),
            }
        for t in sorted(self.by_id.values(), key=lambda t: (t.profile, t.path)):
            slot_values = defaultdict(Counter)
            for e in t.entries:
                for i, v in enumerate(extract_values(e.tokens, t.pattern)):
                    slot_values[i][v] += 1
            slots = []
            for i, (kind, typ) in enumerate(el for el in t.pattern if el[0] != "kw"):
                slot = {"type": typ, "list": kind == "list",
                        "values": dict(slot_values[i].most_common(10))}
                if typ == "ENUM":
                    slot["enum"] = sorted(slot_values[i])
                slots.append(slot)
            out["templates"].append({
                "id": t.id,                       # = hash(vendor_family, dialect, path)
                "vendor_family": t.profile,
                "dialect": t.dialect,
                "parent": t.parent,
                "path": t.path,
                "structured": t.structured,
                "pattern": [list(el) for el in t.pattern],
                "display": render(t.pattern),
                "context": " > ".join(t.path[:-1]),
                "lines": len(t.entries),
                "files": len({e.file for e in t.entries}),
                "negated": sum(e.negated for e in t.entries),
                "slots": slots,
                "examples": pick_examples({"file": e.file, "line": e.line_no, "raw": e.raw,
                                           "negated": e.negated, "tokens": e.tokens,
                                           "blob": e.blob}
                                          for e in t.entries),
            })
        return out


def assign_profiles(parsed, from_folder: bool, threshold: float) -> dict:
    """parsed: [(path, dialect, cmds, vendor)]. Folder name, else the detected vendor
    profile, else similarity clustering."""
    profiles: dict[str, dict] = {}
    for path, dialect, cmds, vendor in parsed:
        sig = file_signature(dialect, cmds)
        if from_folder:
            name = path.parent.name
        elif vendor != "generic":
            name = vendor
        else:
            best, best_sim = None, 0.0
            for pname, p in profiles.items():
                if p["dialect"] != dialect:
                    continue
                sim = max(overlap(sig, s) for s in p["sigs"])
                if sim > best_sim:
                    best, best_sim = pname, sim
            if best is not None and best_sim >= threshold:
                name = best
            else:
                n = sum(1 for p in profiles.values() if p["dialect"] == dialect) + 1
                name = f"{dialect}-{n}"
        p = profiles.setdefault(name, {"dialect": dialect, "members": [], "sigs": []})
        p["members"].append((path, dialect, cmds))
        p["sigs"].append(sig)
    return profiles
