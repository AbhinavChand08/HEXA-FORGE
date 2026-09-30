"""Section D: job model + raw storage (D1), vendor detection ambiguity (D2), dialect
parameter guessing for a brand-new vendor (D3). No database needed."""
import pytest

from app.ingest import jobs
from app.ingest.detector import AMBIGUOUS_MARGIN, OVERLAP_WEIGHT, detect_vendor
from app.ingest.dialect_guess import (guess_comment_prefix, guess_dialect_params,
                                      guess_indent_unit, guess_negation_prefix,
                                      guess_terminators)
from app.ingest.fingerprints import profile_row


# ----------------------------------------------------------------------------- D1
def test_new_id_is_a_sortable_ulid():
    import time
    ids = []
    for _ in range(5):
        ids.append(jobs.new_id())
        time.sleep(0.002)                        # force distinct millisecond timestamps
    assert len(set(ids)) == 5                    # unique
    assert all(len(i) == 26 for i in ids)
    assert sorted(ids) == ids                    # created in order -> already sorted


def test_store_raw_is_content_addressed_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings.OBJECT_STORE_DIR", tmp_path)
    key1, sha1 = jobs.store_raw("rtr-01", b"hello")
    key2, sha2 = jobs.store_raw("rtr-01", b"hello")          # same bytes -> same file, not rewritten
    assert (key1, sha1) == (key2, sha2)
    assert jobs.read_raw(key1) == b"hello"
    _, sha3 = jobs.store_raw("rtr-01", b"world")
    assert sha3 != sha1
    key4, sha4 = jobs.store_raw("../../etc/rtr", b"hello")   # unsafe device name
    assert sha4 == sha1 and ".." not in key4


# ----------------------------------------------------------------------------- D2
def _row(name, weight, threshold=1, dialect="indent"):
    return profile_row(name, {"dialect": dialect, "threshold": threshold,
                              "fingerprints": [{"pattern": f"^{name}-marker", "weight": weight}]})


def test_matched_when_one_profile_clears_its_threshold_with_no_close_rival():
    rows = [_row("alpha", 3), _row("beta", 3)]
    result = detect_vendor("alpha-marker\nsome other line\n", rows)
    assert result["status"] == "MATCHED"
    assert result["profile"]["name"] == "alpha"
    assert result["scores"][0] == {"name": "alpha", "score": 3.0}


def test_needs_vendor_when_the_top_two_scores_are_close():
    rows = [_row("alpha", 3), _row("beta", 3)]
    result = detect_vendor("alpha-marker\nbeta-marker\n", rows)
    assert result["status"] == "NEEDS_VENDOR"
    assert {s["name"] for s in result["scores"]} == {"alpha", "beta"}


def test_unknown_vendor_when_nothing_clears_its_threshold():
    rows = [_row("alpha", 3, threshold=5)]
    result = detect_vendor("nothing recognisable here\n", rows)
    assert result["status"] == "UNKNOWN_VENDOR"
    assert result["profile"]["name"] == "generic"


def test_declared_vendor_is_trusted_but_a_disagreement_is_flagged():
    rows = [_row("alpha", 3), _row("beta", 3)]
    mismatched = detect_vendor("beta-marker\n", rows, declared="alpha")
    assert mismatched["status"] == "MATCHED" and mismatched["profile"]["name"] == "alpha"
    assert mismatched["mismatch"] is True

    agreeing = detect_vendor("alpha-marker\n", rows, declared="alpha")
    assert agreeing["mismatch"] is False


def test_learned_signature_overlap_resolves_a_vendor_with_no_fingerprints_yet():
    """gamma is mid-onboarding (task I): mined templates exist (sigs), but the reviewer
    has not set fingerprints yet. The overlap signal alone should still identify it."""
    gamma = profile_row("gamma", {"dialect": "indent"})
    result = detect_vendor("foo bar\n", [gamma], sigs={"gamma": ("indent", {"foo bar"})})
    assert result["status"] == "MATCHED" and result["profile"]["name"] == "gamma"
    assert result["scores"][0]["score"] == pytest.approx(OVERLAP_WEIGHT)


def test_ambiguous_margin_is_ten_percent():
    assert AMBIGUOUS_MARGIN == pytest.approx(0.10)


# ----------------------------------------------------------------------------- D3
VRP_SAMPLE = """#
sysname HW-CORE-01
#
telnet server enable
undo telnet server enable
#
snmp-agent
undo snmp-agent
#
user-interface vty 0 4
 idle-timeout 5 0
 quit
#
quit
#
return
"""


def test_guess_comment_prefix_is_the_most_common_single_char_line():
    assert guess_comment_prefix(VRP_SAMPLE) == "#"


def test_guess_indent_unit_is_the_common_positive_step():
    assert guess_indent_unit(VRP_SAMPLE) == 1


def test_guess_terminators_excludes_the_comment_marker():
    assert guess_terminators(VRP_SAMPLE, comment_prefix="#") == ["quit"]


def test_guess_negation_prefix_from_lines_seen_both_ways():
    assert guess_negation_prefix(VRP_SAMPLE) == "undo"


def test_guess_dialect_params_combines_every_guess():
    assert guess_dialect_params(VRP_SAMPLE, "indent") == {
        "dialect": "indent", "comment_prefixes": ["#"], "indent_unit": 1,
        "block_terminators": ["quit"], "negation_prefixes": ["undo"]}


def test_guess_dialect_params_is_safe_with_no_signal():
    assert guess_dialect_params("{}", "json") == {"dialect": "json"}
