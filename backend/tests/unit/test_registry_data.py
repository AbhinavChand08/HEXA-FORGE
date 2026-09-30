"""Section C pieces that need no database: profile rows as data (C2), matching with an
inherited profile (C3), example selection (C4), list merge policy (C8)."""
import pytest

from app.core.models import pick_examples
from app.ingest.detector import detect
from app.ingest.fingerprints import GENERIC, load_profiles, profile_row, validate_profile
from app.mapping.compiler import build_index
from app.mapping.matcher import match_config
from app.mapping.normalizer import Normalizer, validate
from app.parsing.base import effective_hints, hints_from_params
from app.parsing.flattener import flatten_config
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer

VRP = """#
sysname HW-CORE-01
#
undo telnet server enable
stelnet server enable
#
user-interface vty 0 4
 idle-timeout 5 0
#
return
"""


def _vrp_row(**params):
    return profile_row("vrp", {
        "dialect": "indent",
        "dialect_params": {"comment_prefixes": ["#"], "block_terminators": ["quit", "return"],
                           **params},
        "fingerprints": [{"pattern": "^sysname ", "weight": 3}, {"pattern": "^undo ", "weight": 3}],
        "threshold": 5})


# ----------------------------------------------------------------------------- C2
def test_seed_profiles_are_valid_rows():
    rows = load_profiles()
    assert {r["name"] for r in rows} >= {"cisco", "junos", "huawei"}
    for r in rows:
        assert validate_profile(r) == [], r["name"]


def test_design_parameter_names_map_to_dialect_hints():
    got = hints_from_params({"block_terminators": ["quit"], "blob_starts": [{"start": "x", "end": "y"}],
                             "secret_patterns": ["psk"], "indent_unit": 1,
                             "negation_prefixes": ["undo"], "by_dialect": {"set": {}}})
    assert got == {"terminators": ["quit"], "literal_blocks": [{"start": "x", "end": "y"}],
                   "secret_keywords": ["psk"], "negation_prefixes": ["undo"]}
    with pytest.raises(ValueError):
        hints_from_params({"no_such_knob": 1})
    junos = next(r for r in load_profiles() if r["name"] == "junos")
    assert effective_hints("set", junos, None).negation_prefixes == ["delete"]
    assert effective_hints("brace", junos, None).negation_prefixes == []


def test_a_profile_row_alone_configures_detection_and_parsing():
    vendor, dialect = detect(VRP, [_vrp_row(negation_prefixes=["undo"])])
    assert (vendor["name"], dialect) == ("vrp", "indent")
    cmds = {c.path_str: c for c in flatten_config(VRP, dialect, vendor)}
    assert cmds["telnet server enable"].negated
    assert "user-interface vty 0 4 > idle-timeout 5 0" in cmds
    assert not any(p.startswith("#") or p == "return" for p in cmds)

    plain = flatten_config(VRP, dialect, _vrp_row(negation_prefixes=[]))
    assert not any(c.negated for c in plain)


def test_fingerprint_threshold():
    row = _vrp_row()
    assert detect("sysname X\n", [row])[0] is GENERIC                 # score 3 < threshold 5
    assert detect("sysname X\nundo ftp\n", [row])[0]["name"] == "vrp"


# ----------------------------------------------------------------------------- C3
def _tpl(vendor, sig, pattern):
    return {"id": sig, "vendor_family": vendor, "parent": None, "structured": False,
            "pattern": pattern}


def test_inherited_templates_match_and_own_templates_win_ties():
    tpls = [_tpl("cisco", "p-ssh", [["kw", "ip"], ["kw", "ssh"], ["kw", "version"], ["slot", "NUM"]]),
            _tpl("cisco", "p-host", [["kw", "hostname"], ["slot", "WORD"]]),
            _tpl("iosxe", "c-host", [["kw", "hostname"], ["slot", "WORD"]])]
    index = build_index(tpls)
    cmds = flatten_config("hostname r1\nip ssh version 2\n", "indent", None)
    lexer = Lexer(LearnerHints().ifname_prefixes)
    got = [t["id"] if t else None
           for _, t, _, _ in match_config(cmds, index, ["iosxe", "cisco"], lexer)]
    assert got == ["c-host", "p-ssh"]
    assert [t for _, t, _, _ in match_config(cmds, index, "iosxe", lexer)][1] is None


# ----------------------------------------------------------------------------- C4
def test_examples_are_spread_over_devices_and_capped():
    exs = [{"device": d, "line": i, "raw": f"x {i}"} for d in "aaaab" for i in range(3)]
    exs = [{**e, "line": n} for n, e in enumerate(exs)]
    got = pick_examples(exs)
    assert len(got) == 5 and {e["device"] for e in got} == {"a", "b"}
    assert got[:2] == [exs[0], exs[12]]
    assert pick_examples(exs[:2] + exs[:2]) == exs[:2]              # duplicates dropped


# ----------------------------------------------------------------------------- C8
KEYS = {"ACL.ENTRIES": {"key": "ACL.ENTRIES", "type": "string", "scope": "GLOBAL",
                        "cardinality": "list"},
        "SSH.VERSION": {"key": "SSH.VERSION", "type": "int", "scope": "GLOBAL",
                        "cardinality": "single"}}


class _Cmd:
    src_line, raw = 1, "x"


def test_merge_policy_append_keeps_order_and_duplicates():
    n = Normalizer.__new__(Normalizer)
    n.keys = KEYS
    t, m = {"signature": "s", "db_id": 1}, {"id": 1}
    for policy, want in (("append", ["b", "a", "b"]), ("union", ["b", "a"])):
        obs = {}
        for v in ("b", "a", "b"):
            n._record(obs, [], "ACL.ENTRIES", "GLOBAL", "", v, t, m, _Cmd(), policy)
        assert obs[("ACL.ENTRIES", "GLOBAL", "")]["value"] == want


def test_merge_is_validated():
    tpl = {"vars": [], "path": ["x"], "pattern": [["kw", "x"]], "examples": []}
    ok = {"emits": [{"key": "ACL.ENTRIES", "value": "'a'", "merge": "append"}]}
    assert validate(ok, tpl, KEYS)[0] == []
    bad_policy = {"emits": [{"key": "ACL.ENTRIES", "value": "'a'", "merge": "concat"}]}
    single = {"emits": [{"key": "SSH.VERSION", "value": "2", "merge": "append"}]}
    assert validate(bad_policy, tpl, KEYS)[0] and validate(single, tpl, KEYS)[0]
