"""Task E3: offline consolidation re-mines a vendor's stored flat records (task E2) and
supersedes runtime-registered unknown templates that a corpus-wide mining pass merges."""
from types import SimpleNamespace

from app.cli.mapper import cmd_consolidate
from app.core.models import FlatCommand
from app.registry import repository as registry
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer, typed_pattern
from app.templating.signature import render
from app.templating.signature import signature as sig_fn

FAMILY = "acme-e3"
LHINTS = LearnerHints()
LEXER = Lexer(LHINTS.ifname_prefixes)


def _cmd(tokens, raw):
    c = FlatCommand(tokens, 1, None, False, raw)
    c.id = 0
    return c


def _register_unknown(conn, tokens, raw):
    """What Normalizer._register_unknown would have stored for one unmapped, unreviewed
    command seen alone (one literal template per distinct value it happens to see)."""
    pattern = typed_pattern(tokens, LEXER.types(tokens), None, LHINTS)
    path = [render(pattern)]
    sig = sig_fn(FAMILY, "indent", path)
    rec = {"signature": sig, "vendor_family": FAMILY, "dialect": "indent",
          "pattern": [list(el) for el in pattern], "path": path, "structured": False,
          "slots": [], "examples": [{"file": "t", "device": "d", "line": 1, "raw": raw,
                                     "negated": False, "tokens": tokens}], "frequency": 1}
    return registry.upsert_template(conn, rec, None)


def _job(conn, jid, device, tokens, raw):
    registry.create_job(conn, jid, device, f"raw/{jid}", jid)
    registry.set_job_detection(conn, jid, FAMILY, "indent", {})
    registry.save_flat_records(conn, jid, [_cmd(tokens, raw)])


def test_consolidate_merges_a_word_slot_a_single_job_never_would(conn):
    old_a = _register_unknown(conn, ["banner-owner", "Server1", "active"],
                              "banner-owner Server1 active")
    old_b = _register_unknown(conn, ["banner-owner", "Server2", "active"],
                              "banner-owner Server2 active")
    conn.commit()
    assert old_a["signature"] != old_b["signature"]     # two literal templates today

    _job(conn, "job-a", "dev-a", ["banner-owner", "Server1", "active"],
        "banner-owner Server1 active")
    _job(conn, "job-b", "dev-b", ["banner-owner", "Server2", "active"],
        "banner-owner Server2 active")
    conn.commit()

    cmd_consolidate(SimpleNamespace(family=FAMILY, hints=None), conn)

    a = registry.get_template(conn, old_a["signature"])
    b = registry.get_template(conn, old_b["signature"])
    assert a["status"] == "merged" and b["status"] == "merged"
    assert a["merged_into"] == b["merged_into"]
    merged = registry.get_template(conn, str(a["merged_into"]))
    assert merged["display"] == "banner-owner <WORD> active"
    assert merged["status"] == "unknown"                # not reviewed, just re-mined

    queue = {t["signature"] for t in registry.review_queue(conn, FAMILY, limit=50)}
    assert old_a["signature"] not in queue and old_b["signature"] not in queue
    assert merged["signature"] in queue


def test_consolidate_is_a_no_op_on_an_already_fully_mined_vendor(conn):
    """Re-running consolidation on a vendor whose corpus already matches the registry
    (task Q's seeded vendors) mines the same signatures back and merges nothing."""
    before = {t["signature"]: t["status"] for t in registry.templates_by_family(conn, "arista")}
    cmd_consolidate(SimpleNamespace(family="arista", hints=None), conn)
    after = {t["signature"]: t["status"] for t in registry.templates_by_family(conn, "arista")}
    assert after == before
