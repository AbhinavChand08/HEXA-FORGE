"""Section E: set-style hierarchy split (E1), blob/src_lines/secret_masked/inactive
flat-record fields (E2), per-vendor lexer/miner data + reserved_keywords (E4)."""
from app.parsing.base import DialectHints
from app.parsing.dialects.brace import parse_brace
from app.parsing.dialects.setstyle import parse_set
from app.parsing.flattener import flatten_config
from app.templating.hints import LearnerHints
from app.templating.lexer import typed_pattern
from app.templating.miner import TemplateLearner

JUNOS_SET = """
set system services ssh protocol-version v2
set system services ssh root-login deny
set system services ssh connection-limit 10
set system services ssh rate-limit 10
delete system services telnet
delete system services ftp
set system time-zone UTC
"""


# ----------------------------------------------------------------------------- E1
def test_set_style_infers_the_same_hierarchy_shared_commands_have():
    cmds = flatten_config(JUNOS_SET, "set", None)
    by_path = {c.path_str: c for c in cmds}

    ssh = by_path["system > services > ssh > protocol-version v2"]
    assert ssh.context == [["system"], ["services"], ["ssh"]]
    assert not ssh.negated

    telnet = by_path["system > services > telnet"]
    assert telnet.context == [["system"], ["services"]]
    assert telnet.negated                              # 'delete' sets negated

    # siblings under the same block share one parent record (not one per line)
    root_login = by_path["system > services > ssh > root-login deny"]
    assert root_login.parent is ssh.parent

    # a prefix seen by only one line never becomes its own block
    tz = by_path["system > time-zone UTC"]
    assert tz.context == [["system"]]


def test_set_style_verb_stripped_before_the_tree_is_built():
    cmds = parse_set("set a b 1\ndelete a b 2\n", DialectHints().update({
        "affirm_prefixes": ["set"], "negation_prefixes": ["delete"]}))
    leaves = {tuple(c.tokens): c for c in cmds if not c.is_block}
    assert leaves[("1",)].parent is leaves[("2",)].parent      # shared 'a > b' block
    assert leaves[("1",)].parent.tokens == ["b"]
    assert not leaves[("1",)].negated and leaves[("2",)].negated


# ----------------------------------------------------------------------------- E2
def test_banner_blob_is_kept_out_of_tokens_and_spans_every_line():
    text = "banner motd ^C\nline one\nline two\n^C\nhostname r1\n"
    cmds = flatten_config(text, "indent", None)
    banner = next(c for c in cmds if c.tokens[:2] == ["banner", "motd"])
    assert banner.tokens == ["banner", "motd"]          # body is not a trailing token
    assert banner.blob == "line one line two"
    assert banner.src_lines == [1, 2, 3, 4]


def test_secret_masking_sets_the_secret_masked_flag():
    cmds = flatten_config("enable secret 5 hunter2\nhostname r1\n", "indent", None)
    enable = next(c for c in cmds if c.tokens[0] == "enable")
    assert enable.secret_masked
    host = next(c for c in cmds if c.tokens[0] == "hostname")
    assert not host.secret_masked


def test_brace_inactive_marker_is_stripped_and_flagged():
    text = "inactive: system {\n host-name r1;\n}\nprotect: routing-options {\n a b;\n}\n"
    cmds = parse_brace(text, DialectHints())
    system = next(c for c in cmds if c.tokens == ["system"])
    assert system.inactive
    routing = next(c for c in cmds if c.tokens == ["routing-options"])
    assert routing.inactive
    child = next(c for c in cmds if c.tokens == ["host-name", "r1"])
    assert not child.inactive                          # only the marked statement itself


# ----------------------------------------------------------------------------- E4
def test_learner_hints_for_profile_overrides_only_given_fields():
    base = LearnerHints()
    profile = {"dialect_params": {"templating_params": {
        "ifname_prefixes": ["xe"], "reserved_keywords": ["area"]}}}
    h = LearnerHints.for_profile(profile, base=base)
    assert h.ifname_prefixes == ["xe"]
    assert h.reserved_keywords == ["area"]
    assert h.value_after == base.value_after            # untouched fields keep the base

    assert LearnerHints.for_profile(None, base=base).ifname_prefixes == base.ifname_prefixes
    base.reserved_keywords.append("mutated")             # for_profile must not alias the base
    assert h.reserved_keywords == ["area"]


def test_reserved_keyword_is_never_a_slot_at_runtime():
    hints = LearnerHints(reserved_keywords=["1"])
    tokens = ["router", "ospf", "1"]
    types = [None, None, "NUM"]
    assert typed_pattern(tokens, types, None, hints) == (
        ("kw", "router"), ("kw", "ospf"), ("kw", "1"))


def test_reserved_keyword_is_never_a_slot_when_mining():
    """Without reserved_keywords, 'ospf 1' would still turn into 'ospf <NUM>' (a typed
    token is always a slot on its own, design 7.3); the vendor can pin the process id."""
    cmds_a = flatten_config("router ospf 1\n", "indent", None)
    cmds_b = flatten_config("router ospf 1\n", "indent", None)

    learner = TemplateLearner(LearnerHints())
    learner.learn_profile("acme", [("a", "indent", cmds_a), ("b", "indent", cmds_b)],
                         profile_row={"dialect_params": {"templating_params": {
                             "reserved_keywords": ["1"]}}})
    displays = {t.path[-1] for t in learner.by_id.values()}
    assert "router ospf 1" in displays
    assert not any(d.startswith("router ospf <") for d in displays)
