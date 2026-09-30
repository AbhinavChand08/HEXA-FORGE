"""Section C: registry version, profiles as data, inheritance, bookkeeping, lifecycle,
export round trip, Alembic upgrade of a pre-Alembic database."""
import re

import yaml
from alembic.script import ScriptDirectory
from psycopg.types.json import Jsonb

from app import settings
from app.db.migrate import MIGRATIONS, config
from app.db.session import apply_schema, connect
from app.ingest.detector import detect
from app.ingest.fingerprints import profile_row
from app.mapping.normalizer import Normalizer, parse_named_template
from app.parsing.flattener import flatten_config
from app.registry import exporter, seed_loader
from app.registry import repository as registry

CISCO = settings.SAMPLES_DIR / "cisco" / "cis-hardened-running-config.cfg"
LEARNED = settings.SEED_DIR / "learned" / "templates.json"


def _value(doc, key, stype="GLOBAL", sid=""):
    return next((o for o in doc["values"]
                 if (o["key"], o["scope_type"], o["scope_id"]) == (key, stype, sid)), None)


# ----------------------------------------------------------------------------- C7
def test_pre_alembic_database_upgrades_in_place(new_db):
    with connect(new_db()) as c:
        c.execute((MIGRATIONS / "versions" / "0001_baseline.sql").read_text())
        c.execute("INSERT INTO vendor_profile (name, family, dialect, fingerprints) "
                  "VALUES ('old', 'old', 'indent', %s)",
                  (Jsonb({"signature": ["ip ssh"], "files": ["a.cfg"]}),))
        c.commit()
        apply_schema(c)
        apply_schema(c)                                  # idempotent
        row = c.execute("SELECT fingerprints, learned, status, threshold FROM vendor_profile "
                        "WHERE name = 'old'").fetchone()
        assert row["fingerprints"] == [] and row["status"] == "onboarding"
        assert row["learned"] == {"dialect": "indent", "signature": ["ip ssh"], "files": ["a.cfg"]}
        head = ScriptDirectory.from_config(config("")).get_current_head()
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == head


# ----------------------------------------------------------------------------- C1
def test_writes_bump_the_version_and_unchanged_seed_does_not(conn):
    from ml.service.embed import Embedder, key_text
    v = registry.current_version(conn)
    assert v > 0
    assert seed_loader.init(conn)["registry_version"] == v
    assert seed_loader.seed_mappings(conn)["done"] == 0
    assert registry.current_version(conn) == v

    keys = seed_loader.ontology()
    keys[0] = {**keys[0], "description": keys[0]["description"] + " (edited)"}
    vectors = Embedder("hash").encode([key_text(k) for k in keys])
    assert registry.upsert_keys(conn, keys, vectors, actor="alice") == v + 1
    change = registry.changes(conn, limit=1)[0]
    assert (change["change_kind"], change["change_ref"], change["actor"]) == \
        ("key", f"key:{keys[0]['key']}", "alice")

    doc = Normalizer(conn).run(CISCO, learn=False)
    assert doc["registry_version"] == v + 1


# ----------------------------------------------------------------------------- C5
def test_rollback_and_reset_change_the_next_normalization(conn):
    t = registry.get_template(conn, "ip ssh version <NUM>", "cisco")
    assert _value(Normalizer(conn).run(CISCO, learn=False), "SSH.VERSION")["value"] == 2

    v0 = registry.current_version(conn)
    saved = registry.save_mapping(conn, t["id"], emits=[{"key": "SSH.VERSION", "value": "v1 + 100"}],
                                  source="human", approved_by="alice", active=True)
    assert saved["version"] == 2 and saved["registry_version"] == v0 + 1
    doc = Normalizer(conn).run(CISCO, learn=False)
    assert _value(doc, "SSH.VERSION")["value"] == 102 and doc["registry_version"] == v0 + 1

    rv = registry.activate_version(conn, t["id"], 1, "alice")
    doc = Normalizer(conn).run(CISCO, learn=False)
    assert _value(doc, "SSH.VERSION")["value"] == 2 and doc["registry_version"] == rv

    registry.reset_to_unknown(conn, t["id"], "alice")
    doc = Normalizer(conn).run(CISCO, learn=False)
    assert t["id"] in [p["template_id"] for p in doc["pending"]]
    assert _value(doc, "SSH.VERSION")["source"] == "default"
    assert registry.get_template(conn, str(t["id"]))["status"] == "unknown"

    h = registry.history(conn, t["id"])
    assert [c["detail"]["action"] for c in reversed(h["changes"])] == \
        ["approve", "approve", "activate", "reset"]
    assert not any(m["active"] for m in h["mappings"])


def test_activate_refuses_suggestions_and_missing_versions(conn):
    t = registry.get_template(conn, "ip ssh version <NUM>", "cisco")
    registry.save_mapping(conn, t["id"], emits=[], source="model")
    for version, error in ((2, ValueError), (9, LookupError)):
        try:
            registry.activate_version(conn, t["id"], version, "alice")
        except error:
            continue
        raise AssertionError(f"version {version} was activated")


# ----------------------------------------------------------------------------- C2
VRP = """#
sysname HW-CORE-01
#
undo telnet server enable
stelnet server enable
#
user-interface vty 0 4
 authentication-mode aaa
 idle-timeout 5 0
#
return
"""


def test_a_profile_row_in_the_database_drives_detection_and_parsing(conn):
    row = profile_row("vrp_test", {
        "family": "huawei", "dialect": "indent",
        "dialect_params": {"comment_prefixes": ["#"], "block_terminators": ["quit", "return"],
                           "negation_prefixes": ["undo"]},
        "fingerprints": [{"pattern": "^sysname ", "weight": 10}, {"pattern": "^undo ", "weight": 10}],
        "threshold": 15})
    v = registry.current_version(conn)
    assert registry.upsert_profiles(conn, [row], actor="alice") == v + 1
    assert registry.upsert_profiles(conn, [row]) is None               # unchanged

    vendor, dialect = detect(VRP, registry.vendor_profiles(conn))
    assert (vendor["name"], dialect) == ("vrp_test", "indent")
    cmds = {c.path_str: c for c in flatten_config(VRP, dialect, vendor)}
    assert cmds["telnet server enable"].negated
    assert "user-interface vty 0 4 > idle-timeout 5 0" in cmds
    assert not any(p.startswith("#") or p == "return" for p in cmds)


# ----------------------------------------------------------------------------- C3
def test_child_profile_matches_with_its_parents_templates_and_defaults(conn):
    parent_doc = Normalizer(conn).run(CISCO, learn=False)
    cisco = next(p for p in registry.vendor_profiles(conn) if p["name"] == "cisco")
    child = profile_row("cisco_iosxe", {
        "family": "cisco", "parent": "cisco", "dialect": "indent",
        "dialect_params": cisco["dialect_params"],
        "fingerprints": [{"pattern": "^service timestamps", "weight": 50}]})
    registry.upsert_profiles(conn, [child])

    doc = Normalizer(conn).run(CISCO, learn=False)
    assert doc["vendor_family"] == "cisco_iosxe" and doc["profile_lineage"] == ["cisco_iosxe", "cisco"]
    assert not doc["new_vendor_family"] and doc["summary"] == parent_doc["summary"]
    strip = lambda d: [{k: v for k, v in o.items() if k != "note"} for o in d["values"]]
    assert strip(doc) == strip(parent_doc)

    default = next(o for o in parent_doc["values"] if o["source"] == "default"
                   and o["scope_type"] == "GLOBAL" and isinstance(o["value"], bool))
    registry.upsert_defaults(conn, [{"vendor_family": "cisco_iosxe", "key": default["key"],
                                     "value": not default["value"]}])
    doc = Normalizer(conn).run(CISCO, learn=False)
    assert _value(doc, default["key"])["value"] is (not default["value"])

    looped = {**profile_row("cisco", {k: cisco[k] for k in
                                      ("family", "dialect", "dialect_params", "fingerprints",
                                       "threshold", "status")}), "parent": "cisco_iosxe"}
    try:
        registry.upsert_profiles(conn, [looped])
    except ValueError as e:
        assert "cycle" in str(e)
    else:
        raise AssertionError("parent cycle accepted")


# ----------------------------------------------------------------------------- C4
def test_device_count_and_examples_spread_over_devices(conn, tmp_path):
    text = CISCO.read_text()
    extra = "".join(f"zzz-feature level {i}\n" for i in range(1, 8))
    paths = []
    for host in ("edge-a", "edge-b"):
        p = tmp_path / f"{host}.cfg"
        p.write_text(re.sub(r"(?m)^hostname \S+", f"hostname {host}", text) + extra)
        paths.append(p)
    ids = set()
    for p in paths:
        doc = Normalizer(conn).run(p, count=True)
        registry.save_device(conn, doc)
        ids |= set(doc["new_template_ids"])

    ssh = registry.get_template(conn, "ip ssh version <NUM>", "cisco")
    assert ssh["device_count"] == 2
    new = registry.get_template(conn, "zzz-feature level <NUM>", "cisco")
    assert new["id"] in ids and new["device_count"] == 2
    assert len(new["examples"]) == 5
    assert {ex["device"] for ex in new["examples"]} == {"edge-a", "edge-b"}
    n = conn.execute("SELECT count(*) AS n FROM job_template WHERE template_id = %s",
                     (ssh["id"],)).fetchone()["n"]
    assert n == 2


# ----------------------------------------------------------------------------- C6
def _mapping_entries(seed_dir):
    return [(m["vendor_family"], parse_named_template(m["template"]), m.get("emits") or [],
             m.get("scope"), bool(m.get("ignore"))) for m in seed_loader.mappings(seed_dir)]


def test_export_round_trips(conn, new_db, tmp_path):
    x, y = tmp_path / "x", tmp_path / "y"
    exporter.export(conn, x)

    seed = settings.SEED_DIR
    by_key = lambda rows: sorted((registry._key_row(r) | {"seed_group": None} for r in rows),
                                 key=lambda r: r["key"])
    assert by_key(seed_loader.ontology(seed)) == by_key(seed_loader.ontology(x))
    assert sorted(map(registry._rule_row, seed_loader.rules(seed)), key=lambda r: r["id"]) == \
        sorted(map(registry._rule_row, seed_loader.rules(x)), key=lambda r: r["id"])
    dflt = lambda rows: {(r["vendor_family"], r["key"], r.get("os_range")): (r["value"], r.get("note"))
                         for r in rows}
    assert dflt(seed_loader.vendor_defaults(seed)) == dflt(seed_loader.vendor_defaults(x))
    assert seed_loader.profiles(seed) == seed_loader.profiles(x)
    assert _mapping_entries(seed) == _mapping_entries(x)
    assert yaml.safe_load((x / "vendors" / "cisco" / "profile.yaml").read_text())["dialect"] == "indent"

    with connect(new_db()) as c2:
        seed_loader.init(c2, x)
        seed_loader.import_templates(c2, LEARNED)
        assert not seed_loader.seed_mappings(c2, x)["warnings"]
        c2.commit()
        exporter.export(c2, y)
    files = lambda d: {p.relative_to(d): p.read_text() for p in sorted(d.rglob("*.yaml"))}
    assert files(x) == files(y)
