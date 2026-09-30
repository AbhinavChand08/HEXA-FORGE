"""Re-rank signals for (template, key) pairs (design §12.4).

G3 additions:
  - fit() now uses a hard incompatibility filter instead of 0.45 multiplier
  - category_prior(): bonus when key category matches approved siblings under same parent
  - same_vendor_family(): neighbour-level bonus for same vendor family
"""
from __future__ import annotations

from ml.service.embed import PH, words

# leaf words that usually carry no compliance meaning (prior for the IGNORE candidate)
IGNORE_HINTS = {"aliases": ["description", "remark", "comment", "hostname", "host-name",
                            "sysname", "location", "contact", "domain-name", "alias",
                            "chassis-id"]}
# leaf words that make a command the OFF switch of a feature: 'management ssh > shutdown'
# means SSH.ENABLED = False (a key named after the switch, INTERFACE.SHUTDOWN, is not inverted)
INVERSE_WORDS = {"shutdown", "disable", "disabled", "deny", "inactive"}
# first word of a block header that opens an instance of each scope type
SCOPE_WORDS = {"LINE": {"line", "user-interface", "console", "vty", "aux"},
               "INTERFACE": {"interface", "interfaces", "port"},
               "USER": {"username", "user", "local-user"},
               "VRF": {"vrf", "ip-vrf", "vpn-instance"},
               "ROUTING_INSTANCE": {"routing-instance", "routing-instances"},
               "VLAN": {"vlan", "vlans"},
               "ACL": {"access-list", "acl", "ip-access-list", "filter"}}

# Slot types that are strictly numeric/address — cannot produce int/float via a plain cast.
# Used by the hard type-compatibility filter (G3).
_IP_TYPES = frozenset({"IPV4", "IPV6", "CIDR", "MASK", "MAC"})
_INT_TYPES = frozenset({"NUM", "ENUM"})


def keywords(text: str) -> set[str]:
    """Command words only (no slots): 'ip ssh time-out <NUM>' -> {ip, ssh, time, out}"""
    return {w for tok in text.split() if not PH.match(tok) and tok != ">"
            for w in words(tok).split()}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def lexical(tpl, key) -> float:
    """How well one of the key's aliases matches: covered by the path, similar to the leaf."""
    leaf = keywords(tpl["path"][-1])
    path = keywords(" ".join(tpl["path"]))
    best = 0.0
    for alias in key.get("aliases") or []:
        a = keywords(alias)
        if a and leaf:
            best = max(best, 0.5 * len(a & path) / len(a) + 0.5 * _jaccard(a, leaf))
    return best


def agreement(tpl, other) -> float:
    """Keyword agreement of two template paths; block headers only agree with blocks."""
    a = _jaccard(keywords(" ".join(tpl["path"])), keywords(" ".join(other["path"]))) ** 0.5
    return a * (1.0 if bool(tpl.get("is_block")) == bool(other.get("is_block")) else 0.7)


def depth(tpl) -> int:
    return len(tpl["path"]) - 1


def own_vars(tpl) -> list[dict]:
    return [v for v in tpl["vars"] if v["level"] == depth(tpl)]


def fit(tpl, key) -> tuple[float, list[str]]:
    """Type and scope fit score.

    G3: the type check is now a *hard filter*: truly incompatible slot/key type pairs
    yield 0.0 instead of 0.45 so they can never appear in the ranked list.
    Compatible-but-not-ideal pairs keep a small score (0.7).
    """
    own = {v["type"] for v in own_vars(tpl)}
    parent_slots = any(v["level"] < depth(tpl) for v in tpl["vars"])
    t = key["type"]
    why: list[str] = []

    # ----- hard type compatibility (G3) -----
    # Truly incompatible: e.g. only IP-address slots present but key expects int/float
    # or only numeric slots present but key expects an IP address.
    incompatible = False
    if own:  # if there are own (leaf-level) slots
        if t == "int" and own <= _IP_TYPES:
            incompatible = True  # no NUM/ENUM, only address types
        elif t == "float" and own <= _IP_TYPES:
            incompatible = True
        elif t == "ip" and own <= (_INT_TYPES | frozenset({"WORD", "QSTR", "TEXT"})):
            incompatible = True
        elif t == "cidr" and own <= (_INT_TYPES | frozenset({"WORD", "QSTR", "TEXT"})):
            incompatible = True
    if incompatible:
        why.append(f"{t} key but slots are {sorted(own)} — hard incompatible")
        return 0.0, why

    # ----- graded type fit -----
    type_fit = {
        "int":    1.0 if "NUM" in own else 0.85 if "ENUM" in own else 0.7,
        "float":  1.0 if "NUM" in own else 0.7,
        "ip":     1.0 if own & {"IPV4", "IPV6"} else 0.7,
        "cidr":   1.0 if own & {"CIDR", "IPV4"} else 0.7,
        "bool":   1.0 if not own or "ENUM" in own else 0.7,
        "enum":   1.0,
        "string": 1.0 if own - {"SECRET"} or key["cardinality"] == "list" else 0.7,
    }[t]

    scope_ok = (not parent_slots) if key["scope"] == "GLOBAL" else (parent_slots or bool(own))
    if type_fit < 1:
        why.append(f"{t} key but slots are {sorted(own) or 'none'}")
    if not scope_ok:
        why.append(f"{key['scope']} key vs template scope")
    return type_fit * (1.0 if scope_ok else 0.7), why


def inverted(tpl, key) -> bool:
    """The template switches the key's feature off (see INVERSE_WORDS)."""
    return bool(keywords(tpl["path"][-1]) & INVERSE_WORDS) and \
        not keywords(key["key"]) & INVERSE_WORDS


def _opener(path) -> str:
    toks = path[-1].split()
    return toks[0].lower() if toks else ""


def scope_fits(tpl, neighbour) -> bool:
    word = _opener(tpl["path"])
    return word == _opener(neighbour["path"]) or \
        word in SCOPE_WORDS.get(neighbour["scope"]["type"], ())


# --------------------------------------------------------------------------- G3 new signals
def category_prior(sibling_cats: dict[str, int], key: dict) -> float:
    """Fraction of already-mapped sibling templates (same parent) whose keys share the
    same category as this candidate key (design §12.4 category_prior, G3)."""
    cat = key.get("category")
    if not sibling_cats or not cat:
        return 0.0
    total = sum(sibling_cats.values())
    return sibling_cats.get(cat, 0) / total if total else 0.0


def same_vendor_family(tpl: dict, neighbour: dict) -> bool:
    """True when the kNN neighbour comes from the same vendor family as the query
    template (design §12.4 same_vendor_family, G3)."""
    return bool(tpl.get("vendor_family") and
                tpl["vendor_family"] == neighbour.get("vendor_family"))
