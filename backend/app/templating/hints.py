"""Learner / lexer knobs, all tunable from hints.json (the training UI can append to these)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class LearnerHints:
    # the word after these is always a value:  hostname X, edit X, username X
    value_after: list[str] = field(default_factory=lambda: [
        "hostname", "host-name", "sysname", "username", "name", "edit", "interface",
        "community"])
    # everything after these is one free-text value
    text_after: list[str] = field(default_factory=lambda: [
        "description", "remark", "alias", "comment", "comments", "location", "contact"])
    # everything after these is a list of values (admin can add more)
    list_after: list[str] = field(default_factory=list)
    # interface stems recognised without a '/' (GigabitEthernet0/1 needs none)
    ifname_prefixes: list[str] = field(default_factory=lambda: [
        "ethernet", "fastethernet", "gigabitethernet", "tengigabitethernet",
        "twentyfivegige", "fortygigabitethernet", "hundredgige", "hundredgigabitethernet",
        "port-channel", "loopback", "vlan", "vlanif", "tunnel", "management", "mgmt",
        "serial", "null", "dialer", "bundle-ether", "eth-trunk", "meth", "nve", "vxlan",
        "ge", "xe", "et", "ae", "lo", "irb", "fxp", "em", "eth", "swp", "bond", "br",
        "po", "gi", "fa", "te", "hu", "port"])
    # words that are never a slot, however they vary across a vendor's corpus (design 7.3)
    reserved_keywords: list[str] = field(default_factory=list)
    cluster_threshold: float = 0.6      # file similarity to join a profile
    list_chain_ratio: float = 0.8       # option-word list detection
    block_similarity: float = 0.5       # sibling blocks with similar children
    value_word_min_keys: int = 3        # enable/disable style value words
    enum_max_values: int = 8            # more distinct values than this -> <WORD>
    enum_min_scopes: int = 3            # scopes needed to trust an exclusive choice
    drain_sim_th: float = 0.4
    drain_depth: int = 4

    @classmethod
    def load(cls, path: str | None) -> "LearnerHints":
        hints = cls()
        if path:
            data = json.loads(Path(path).read_text())
            known = {f.name for f in fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(hints, key, value)
        return hints

    @classmethod
    def for_profile(cls, profile: dict | None, base: "LearnerHints | None" = None) -> "LearnerHints":
        """base (or the defaults), overridden by the vendor profile's
        dialect_params.templating_params (design 7.2, 7.3): ifname_prefixes, value_after,
        text_after, list_after and reserved_keywords are vendor data, not global code."""
        hints = copy.deepcopy(base) if base is not None else cls()
        params = ((profile or {}).get("dialect_params") or {}).get("templating_params") or {}
        known = {f.name for f in fields(cls)}
        for key, value in params.items():
            if key in known:
                setattr(hints, key, value)
        return hints
