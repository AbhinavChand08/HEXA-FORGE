"""
rerank.py  -  predicts the mapping of a new template for human review (design §12.3–12.6)

Scoring formula (G3 updated):
  knn_max   highest similarity among neighbours voting for this key (§12.4 sim_neighbour_max)
  votes     weighted vote share: fraction of total adjusted-similarity mass for this key (G3)
  zero_shot similarity to key description or lexical alias match (§12.4 sim_key_desc)
  type_fit  0.0 (hard incompatible), 0.7 (compatible-ish), 1.0 (ideal) — G3 hard filter
  scope_fit 0.7 penalty when scope does not match the template shape
  cat_bonus up to +0.05 when key category matches majority of approved siblings (G3)
  svf_bonus +0.03 when the best neighbour is from the same vendor family (G3)

  base  = max(knn_max, ZS_WEIGHT * zero_shot) + 0.1 * min(knn_max, zero_shot)
          + VOTE_WEIGHT * votes
  score = min(1.0, base * type_fit * scope_fit + cat_bonus + svf_bonus)
"""
from __future__ import annotations

import re
from collections import Counter

from app.mapping import transforms as T
from app.mapping.normalizer import validate
from app.registry import repository as registry
from ml.service import retrieve
from ml.service.embed import PH, Embedder
from ml.service.features import (IGNORE_HINTS, category_prior, fit, inverted, lexical,
                                  own_vars, same_vendor_family)

ZS_WEIGHT = 0.85
VOTE_WEIGHT = 0.15    # weight for neighbour_votes (weighted vote share, G3)
CAT_BONUS = 0.05      # category_prior bonus cap (G3)
SVF_BONUS = 0.03      # same_vendor_family bonus (G3)


# =============================================================================
# Proposals (unchanged from original)
# =============================================================================
def _pick(tpl, prefs) -> dict | None:
    for v in reversed(own_vars(tpl)):
        if v["type"] in prefs:
            return v
    return None


def scope_id(tpl) -> str:
    """'{v1}' placeholders of the nearest ancestor with slots, minus its leading keyword:
    line vty <NUM> <NUM>  ->  'vty {v1} {v2}';  interface <IFNAME>  ->  '{v1}'"""
    n, levels = 0, []
    for text in tpl["path"]:
        toks = []
        for tok in text.split():
            if PH.match(tok):
                n += 1
                toks.append(f"{{v{n}}}")
            else:
                toks.append(tok)
        levels.append(toks)
    for toks in reversed(levels[:-1]):
        if any(t.startswith("{v") for t in toks):
            return " ".join(toks[1:] if len(toks) > 1 else toks)
    own = own_vars(tpl)
    return f"{{{own[0]['name']}}}" if own else " ".join(levels[0][1:] or levels[0])


def scope_id_leaf(tpl) -> str:
    """Scope id for a block header that declares an instance: its own slots."""
    own = own_vars(tpl)
    toks = tpl["path"][-1].split()
    names = iter(v["name"] for v in own)
    out = [f"{{{next(names)}}}" if PH.match(t) else t for t in toks]
    return " ".join(out[1:] if len(out) > 1 else out)


def _rebind(neighbour, tpl) -> dict[str, str]:
    """Neighbour variable -> this template's variable, matched by (leaf level?, type, ordinal)."""
    def ordinals(t):
        d, seen, out = len(t["path"]) - 1, Counter(), []
        for v in t["vars"]:
            k = (v["level"] == d, v["type"])
            out.append(((k, seen[k]), v["name"]))
            seen[k] += 1
        return out
    mine = dict(ordinals(tpl))
    names = neighbour.get("slot_names") or []
    out = {}
    for i, (k, name) in enumerate(ordinals(neighbour)):
        if k in mine:
            out[name] = mine[k]
            if i < len(names) and names[i]:
                out[names[i]] = mine[k]
    return out


def _copy(neighbour, tpl) -> list[dict] | None:
    """The neighbour's emits rewritten onto this template's slots, or None if a slot is missing."""
    binding = _rebind(neighbour, tpl)
    slot_vars = {v["name"] for v in neighbour["vars"]} | set(neighbour.get("slot_names") or [])
    emits = []
    for e in neighbour["emits"]:
        exprs = {f: e[f] for f in ("value", "when") if e.get(f)}
        if any((T.free_names(x) & slot_vars) - binding.keys() for x in exprs.values()):
            return None
        emits.append({**e, **{f: T.rename(x, binding) for f, x in exprs.items()}})
    return emits


def _negate(expr: str) -> str:
    return {"not negated": "negated", "negated": "not negated"}.get(expr, f"not ({expr})")


def propose(tpl, key, neighbour=None) -> dict:
    """A mapping for (template, key): the neighbour's transform rebound, else derived from
    slot types. Bool transforms follow the command's polarity."""
    scope = {"type": key["scope"], "id": scope_id(tpl)} if key["scope"] != "GLOBAL" else None
    if neighbour and neighbour.get("emits"):
        emits = _copy(neighbour, tpl)
        if emits is not None:
            if key["type"] == "bool" and inverted(tpl, key) != inverted(neighbour, key):
                emits = [{**e, "value": _negate(e["value"])} if e["key"] == key["key"] else e
                         for e in emits]
            return {"emits": emits, "scope": scope, "slot_names": [],
                    "how": f"copied from {neighbour['vendor_family']}: {neighbour['display']}"}

    t, name = key["type"], key["key"]
    v = None
    if t == "bool":
        v = _pick(tpl, {"ENUM"})
        expr = f"boolean({v['name']})" if v else "negated" if inverted(tpl, key) else "not negated"
    elif t in ("int", "float"):
        v = _pick(tpl, {"NUM"}) or _pick(tpl, {"ENUM"})
        expr = v["name"] if v else "tokens[-1]"
        if v and v["type"] == "ENUM":
            expr = (f"syslog_level({v['name']})" if "SEVERITY" in name or "LEVEL" in name
                    else f"enum_map({v['name']}, {{}})")
        elif v and key.get("unit") == "seconds" and re.search(r"minute|\bmin\b", tpl["display"]):
            expr = f"to_seconds({v['name']}, 'min')"
    elif t == "ip":
        v = _pick(tpl, {"IPV4", "IPV6"})
        expr = v["name"] if v else "tokens[-1]"
    elif t == "cidr":
        v, mask = _pick(tpl, {"CIDR", "IPV4"}), _pick(tpl, {"MASK"})
        expr = (f"{v['name']} + '/' + str(mask_to_prefix({mask['name']}))"
                if v and mask and v["type"] == "IPV4" else v["name"] if v else "tokens[-1]")
    else:
        v = _pick(tpl, {"ENUM", "WORD", "QSTR", "TEXT", "IFNAME", "IPV4", "IPV6", "NUM", "SECRET"})
        expr = v["name"] if v else "tokens[-1]"
        if v and ("SEVERITY" in name or "LEVEL" in name):
            expr = f"syslog_level({v['name']})"
    if v and v.get("list") and key["cardinality"] == "list":
        expr = f"split({v['name']})"
    return {"emits": [{"key": name, "value": expr}], "scope": scope, "slot_names": [],
            "how": "derived from slot types"}


# =============================================================================
# Suggester
# =============================================================================
class Suggester:
    def __init__(self, conn, embedder: Embedder | None = None):
        self.conn = conn
        self._embedder = embedder
        self.keys = registry.keys(conn)

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder()
        return self._embedder

    def ensure_embeddings(self, ids=None) -> int:
        return retrieve.ensure_embeddings(self.conn, self.embedder, ids)

    def candidates(self, tpl: dict, k: int = 3) -> list[dict]:
        """Rank candidate keys (G3: uses weighted vote share, category_prior,
        same_vendor_family and a hard type filter)."""
        knn_max, knn_votes, ignore, scope_only = retrieve.neighbour_votes(self.conn, tpl)
        zero_shot = retrieve.key_matches(self.conn, tpl, self.keys)
        sib_cats = retrieve.sibling_categories(self.conn, tpl)  # G3 category_prior

        out = []
        for name in (set(knn_max) | set(zero_shot)) & self.keys.keys():
            key = self.keys[name]
            ks, nb = knn_max.get(name, (0.0, None))
            votes = knn_votes.get(name, 0.0)
            zs = zero_shot.get(name, 0.0)
            f, why = fit(tpl, key)         # G3: f = 0.0 for hard incompatible types
            if f == 0.0:
                continue                   # hard filter: skip entirely

            base = (max(ks, ZS_WEIGHT * zs) + 0.1 * min(ks, zs)
                    + VOTE_WEIGHT * votes)
            # G3 bonuses (additive, small — reward but don't dominate)
            cat_b = CAT_BONUS * category_prior(sib_cats, key)
            svf_b = SVF_BONUS if (nb and same_vendor_family(tpl, nb)) else 0.0
            score = min(1.0, base * f + cat_b + svf_b)

            reasons = why + ([f"like {nb['vendor_family']}: {nb['display']} ({ks:.2f})"]
                             if nb else [])
            if nb and same_vendor_family(tpl, nb):
                reasons.append(f"same vendor family ({nb['vendor_family']})")
            out.append(self._candidate("key", name, score, ks, votes, zs, f, reasons,
                                       propose(tpl, key, nb), tpl))

        ign_lex = lexical(tpl, IGNORE_HINTS)
        if ignore[1] or ign_lex > 0:
            n = ignore[1]
            score = max(ignore[0], ZS_WEIGHT * ign_lex)
            reasons = ([f"like ignored {n['vendor_family']}: {n['display']} ({ignore[0]:.2f})"]
                       if n else []) + (["free-text / identity field"] if ign_lex > 0.5 else [])
            out.append({"kind": "ignore", "key": "IGNORE", "score": round(score, 3),
                        "knn": round(ignore[0], 3), "votes": 0.0,
                        "zero_shot": round(ign_lex, 3), "fit": 1.0,
                        "reasons": reasons, "proposal": None, "preview": [], "errors": []})
        if scope_only[1] and own_vars(tpl) and tpl.get("is_block"):
            n = scope_only[1]
            prop = {"emits": [], "scope": {"type": n["scope"]["type"], "id": scope_id_leaf(tpl)},
                    "slot_names": [], "how": f"scope like {n['vendor_family']}: {n['display']}"}
            out.append(self._candidate("scope", f"SCOPE:{n['scope']['type']}", scope_only[0],
                                       scope_only[0], 0.0, 0.0, 1.0, [prop["how"]], prop, tpl))
        out.sort(key=lambda c: -c["score"])
        return out[:k]

    def _candidate(self, kind, name, score, ks, votes, zs, f, reasons, proposal, tpl) -> dict:
        mapping = {k: proposal[k] for k in ("emits", "scope", "slot_names")}
        errors, preview = validate(mapping, tpl, self.keys)
        if errors:
            score *= 0.5
        return {"kind": kind, "key": name, "score": round(score, 3),
                "knn": round(ks, 3), "votes": round(votes, 3),
                "zero_shot": round(zs, 3), "fit": round(f, 2),
                "reasons": reasons, "proposal": proposal, "errors": errors,
                "preview": [[k, list(w), v] for k, w, v, _ in preview]}

    def suggest(self, tpl: dict, k: int = 3) -> dict | None:
        """Rank candidates and store the best as an inactive model mapping (status SUGGESTED).
        Called by the CLI; the HTTP service (main.py) does not store—it returns candidates only."""
        self.ensure_embeddings([tpl["id"]])
        cands = self.candidates(tpl, k)
        if not cands:
            return None
        best = cands[0]
        prop = best["proposal"] or {"emits": [], "scope": None, "slot_names": []}
        registry.save_mapping(
            self.conn, tpl["id"], emits=prop["emits"], scope=prop["scope"],
            slot_names=prop["slot_names"], source="model", confidence=best["score"],
            evidence={"top": best["key"], "candidates": cands, "embedder": self.embedder.name})
        return best
