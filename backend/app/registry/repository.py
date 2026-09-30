"""
repository.py  -  every registry read and write (psycopg 3 + pgvector)

mapping/, compliance/ and learning/ reach the database only through this module.
Writes to registry knowledge (profiles, keys, defaults, rules, mapping decisions) insert a
registry_version row in the caller's transaction and return the new version (None when
an upsert changed nothing); see versioning.py.
"""
from __future__ import annotations

from collections import Counter

from psycopg.types.json import Jsonb

from app.core.models import pick_examples
from app.registry.versioning import bump, changes, current_version  # noqa: F401 (re-export)


def vec(v) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _bump_batch(conn, kind: str, refs: list[str], actor, action: str = "upsert") -> int | None:
    if not refs:
        return None
    ref = refs[0] if len(refs) == 1 else f"{len(refs)} {kind}s"
    return bump(conn, kind, ref, actor, {"action": action, "refs": refs})


# =============================================================================
# Vendor profiles
# =============================================================================
PROFILE_COLS = """p.id, p.name, p.family, pp.name AS parent, p.dialect, p.dialect_params,
    p.fingerprints, p.threshold, p.status, p.learned"""
PROFILE_COMPARED = ("family", "parent", "dialect", "dialect_params", "fingerprints",
                    "threshold", "status")


def vendor_profiles(conn) -> list[dict]:
    """Profile rows (the ingest.fingerprints row shape, plus id and learned), by name."""
    return conn.execute(f"""SELECT {PROFILE_COLS} FROM vendor_profile p
                            LEFT JOIN vendor_profile pp ON pp.id = p.parent_id
                            ORDER BY p.name""").fetchall()


def lineage(profiles, name: str | None) -> list[str]:
    """[name, parent, grandparent, ...] over profile rows; [] when name is not a profile."""
    by_name = {p["name"]: p for p in profiles}
    out: list[str] = []
    while name in by_name and name not in out:
        out.append(name)
        name = by_name[name].get("parent")
    return out


def upsert_profiles(conn, rows: list[dict], actor: str | None = "seed") -> int | None:
    """Reviewed profile fields (full rows, see ingest.fingerprints.profile_row). The learned
    routing signature is left alone. Parents may be rows of this batch or existing ones."""
    before = {r["name"]: r for r in vendor_profiles(conn)}
    known = set(before) | {r["name"] for r in rows}
    changed = []
    for r in rows:
        if r.get("parent") and r["parent"] not in known:
            raise ValueError(f"profile {r['name']}: unknown parent {r['parent']!r}")
        old = before.get(r["name"])
        if old and all(old[f] == r[f] for f in PROFILE_COMPARED):
            continue
        conn.execute(
            """INSERT INTO vendor_profile (name, family, dialect, dialect_params, fingerprints,
                                           threshold, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET family = EXCLUDED.family,
                   dialect = EXCLUDED.dialect, dialect_params = EXCLUDED.dialect_params,
                   fingerprints = EXCLUDED.fingerprints, threshold = EXCLUDED.threshold,
                   status = EXCLUDED.status""",
            (r["name"], r["family"], r["dialect"], Jsonb(r["dialect_params"]),
             Jsonb(r["fingerprints"]), r["threshold"], r["status"]))
        changed.append(r)
    for r in changed:
        conn.execute("""UPDATE vendor_profile SET parent_id =
                            (SELECT id FROM vendor_profile WHERE name = %s)
                        WHERE name = %s""", (r.get("parent"), r["name"]))
    after = vendor_profiles(conn)
    for r in changed:
        chain = lineage(after, r["name"])
        parent = next(p["parent"] for p in after if p["name"] == chain[-1])
        if parent is not None:
            raise ValueError(f"profile {r['name']}: parent cycle {' -> '.join(chain)} -> {parent}")
    return _bump_batch(conn, "profile", [f"profile:{r['name']}" for r in changed], actor)


def record_learned(conn, name: str, dialect: str, signature, files) -> None:
    """Learner output for routing (not reviewed knowledge: no version bump). Creates an
    onboarding profile when the vendor has none yet."""
    learned = {"dialect": dialect, "signature": sorted(signature), "files": list(files)}
    conn.execute(
        """INSERT INTO vendor_profile (name, family, dialect, learned) VALUES (%s, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET learned = EXCLUDED.learned""",
        (name, name, dialect, Jsonb(learned)))


def profile_signatures(conn) -> dict:
    """{vendor id: (dialect, learned top-level signature)} for routing a new config."""
    return {r["name"]: (r["learned"].get("dialect") or r["dialect"],
                        set(r["learned"].get("signature", [])))
            for r in vendor_profiles(conn) if r["learned"].get("signature")}


# =============================================================================
# Ontology
# =============================================================================
KEY_COMPARED = ("type", "unit", "scope", "cardinality", "category", "description",
                "enum_values", "aliases", "allowed")


def _key_row(k: dict) -> dict:
    return {"key": k["key"], "type": k["type"], "unit": k.get("unit"),
            "scope": k.get("scope", "GLOBAL"), "cardinality": k.get("cardinality", "single"),
            "category": k["category"], "description": k["description"],
            "enum_values": k.get("enum_values") or None, "aliases": k.get("aliases", []),
            "seed_group": k.get("seed_group"), "allowed": k.get("allowed") or None}


def upsert_keys(conn, keys_in: list[dict], vectors, actor: str | None = "seed") -> int | None:
    """Embeddings are always refreshed; only a change of meaning bumps the version."""
    before = keys(conn)
    changed = []
    for k, v in zip(map(_key_row, keys_in), vectors):
        old = before.get(k["key"])
        if old is None or any(old[f] != k[f] for f in KEY_COMPARED):
            changed.append(f"key:{k['key']}")
        conn.execute(
            """INSERT INTO canonical_key (key, type, unit, scope, cardinality, category,
                                          description, enum_values, aliases, seed_group,
                                          allowed, embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
               ON CONFLICT (key) DO UPDATE SET type = EXCLUDED.type, unit = EXCLUDED.unit,
                   scope = EXCLUDED.scope, cardinality = EXCLUDED.cardinality,
                   category = EXCLUDED.category, description = EXCLUDED.description,
                   enum_values = EXCLUDED.enum_values, aliases = EXCLUDED.aliases,
                   seed_group = EXCLUDED.seed_group, allowed = EXCLUDED.allowed,
                   embedding = EXCLUDED.embedding""",
            (k["key"], k["type"], k["unit"], k["scope"], k["cardinality"], k["category"],
             k["description"], Jsonb(k["enum_values"]) if k["enum_values"] else None,
             Jsonb(k["aliases"]), k["seed_group"],
             Jsonb(k["allowed"]) if k["allowed"] else None, vec(v)))
    return _bump_batch(conn, "key", changed, actor)


def keys(conn) -> dict[str, dict]:
    return {r["key"]: r for r in conn.execute(
        """SELECT key, type, unit, scope, cardinality, category, description, enum_values, aliases,
                  seed_group, allowed
           FROM canonical_key ORDER BY key""")}


# =============================================================================
# Templates
# =============================================================================
def path_vars(parent_vars: list[dict], slots: list[dict], level: int) -> list[dict]:
    """Every slot of the template path, parents first: v1..vn with a sample value."""
    out = list(parent_vars)
    for s in slots:
        sample = next(iter(s.get("values") or {}), "")
        out.append({"name": f"v{len(out) + 1}", "type": s["type"], "list": s.get("list", False),
                    "level": level, "sample": sample})
    return out


def existing_signatures(conn) -> dict[str, dict]:
    return {r["signature"]: r for r in conn.execute(
        "SELECT id, signature, vars, embedding IS NOT NULL AS has_embedding FROM template")}


def _example_id(ex: dict) -> tuple:
    return ex.get("device") or ex.get("file"), ex.get("line"), ex.get("raw")


def _merge_examples(old, new) -> list[dict]:
    """old + new (a re-seen example is refreshed in place), capped and spread over devices."""
    merged = {_example_id(ex): ex for ex in old or []}
    merged.update({_example_id(ex): ex for ex in new})
    return pick_examples(merged.values())


def add_examples(conn, template_id: int, examples: list[dict]) -> bool:
    """Merge examples into a stored template. -> True when its examples changed"""
    old = conn.execute("SELECT examples FROM template WHERE id = %s FOR UPDATE",
                       (template_id,)).fetchone()["examples"]
    merged = _merge_examples(old, examples)
    if merged == old:
        return False
    conn.execute("UPDATE template SET examples = %s WHERE id = %s", (Jsonb(merged), template_id))
    return True


def upsert_template(conn, rec: dict, parent: dict | None, embedding=None,
                    add_frequency: bool = False) -> dict:
    """rec: learner template (signature, vendor_family, dialect, pattern, path, structured,
    slots, examples, frequency). parent: {'id', 'vars'} row or None. Status is never touched.
    Stored examples are merged with rec's (see _merge_examples)."""
    level = len(rec["path"]) - 1
    tvars = path_vars(parent["vars"] if parent else [], rec["slots"], level)
    old = conn.execute("SELECT examples FROM template WHERE signature = %s",
                       (rec["signature"],)).fetchone()
    examples = _merge_examples(old["examples"] if old else [], rec["examples"])
    freq_sql = ("template.frequency + EXCLUDED.frequency" if add_frequency
                else "GREATEST(template.frequency, EXCLUDED.frequency)")
    return conn.execute(
        f"""INSERT INTO template (vendor_family, dialect, parent_id, signature, pattern, path,
                                  display, structured, slots, vars, examples, frequency, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            ON CONFLICT (signature) DO UPDATE SET slots = EXCLUDED.slots, vars = EXCLUDED.vars,
                examples = EXCLUDED.examples, frequency = {freq_sql}, last_seen = now(),
                embedding = COALESCE(template.embedding, EXCLUDED.embedding)
            RETURNING id, signature, vars, status, (xmax = 0) AS inserted""",
        (rec["vendor_family"], rec["dialect"], parent["id"] if parent else None,
         rec["signature"], Jsonb(rec["pattern"]), Jsonb(rec["path"]), " > ".join(rec["path"]),
         rec["structured"], Jsonb(rec["slots"]), Jsonb(tvars), Jsonb(examples),
         rec["frequency"], vec(embedding))).fetchone()


def bump_frequency(conn, counts: Counter) -> None:
    for tid, n in counts.items():
        conn.execute("UPDATE template SET frequency = frequency + %s, last_seen = now() WHERE id = %s",
                     (n, tid))


TEMPLATE_COLS = """t.id, t.signature, t.vendor_family, t.dialect, p.signature AS parent_signature,
    t.parent_id, t.pattern, t.path, t.display, t.structured, t.slots, t.vars, t.examples,
    t.frequency, t.device_count, t.status, t.merged_into,
    EXISTS (SELECT 1 FROM template c WHERE c.parent_id = t.id) AS is_block"""


def all_templates(conn) -> list[dict]:
    return conn.execute(f"SELECT {TEMPLATE_COLS} FROM template t "
                        "LEFT JOIN template p ON p.id = t.parent_id").fetchall()


def get_template(conn, ref: str, family: str | None = None) -> dict | None:
    """ref: numeric id, signature, or full display path (with family)."""
    sql = f"SELECT {TEMPLATE_COLS} FROM template t LEFT JOIN template p ON p.id = t.parent_id WHERE "
    if ref.isdigit():
        return conn.execute(sql + "t.id = %s", (int(ref),)).fetchone()
    row = conn.execute(sql + "t.signature = %s", (ref,)).fetchone()
    if row or family is None:
        return row
    return conn.execute(sql + "t.vendor_family = %s AND t.display = %s", (family, ref)).fetchone()


def review_queue(conn, family: str | None = None, limit: int = 20,
                 statuses=("unknown", "suggested")) -> list[dict]:
    return conn.execute(
        f"""SELECT {TEMPLATE_COLS} FROM template t LEFT JOIN template p ON p.id = t.parent_id
            WHERE t.status = ANY(%s) AND (%s::text IS NULL OR t.vendor_family = %s)
            ORDER BY t.frequency DESC, t.id LIMIT %s""",
        (list(statuses), family, family, limit)).fetchall()


def neighbours(conn, template_id: int, k: int = 12) -> list[dict]:
    """Nearest reviewed templates (any vendor) with their active mapping."""
    return conn.execute(
        """SELECT t.id, t.vendor_family, t.display, t.path, t.status, t.vars, m.emits, m.scope,
                  m.slot_names, 1 - (t.embedding <=> q.embedding) AS sim,
                  EXISTS (SELECT 1 FROM template c WHERE c.parent_id = t.id) AS is_block
           FROM template t
           CROSS JOIN (SELECT embedding FROM template WHERE id = %s) q
           LEFT JOIN mapping m ON m.template_id = t.id AND m.active
           WHERE t.id <> %s AND t.status IN ('approved', 'ignored') AND t.embedding IS NOT NULL
           ORDER BY t.embedding <=> q.embedding LIMIT %s""",
        (template_id, template_id, k)).fetchall()


def key_similarity(conn, template_id: int, k: int = 12) -> list[dict]:
    return conn.execute(
        """SELECT c.key, 1 - (c.embedding <=> q.embedding) AS sim
           FROM canonical_key c CROSS JOIN (SELECT embedding FROM template WHERE id = %s) q
           WHERE c.embedding IS NOT NULL AND q.embedding IS NOT NULL
           ORDER BY c.embedding <=> q.embedding LIMIT %s""",
        (template_id, k)).fetchall()


def missing_embeddings(conn, ids=None) -> list[dict]:
    return conn.execute(
        """SELECT id, path FROM template WHERE embedding IS NULL
           AND (%s::bigint[] IS NULL OR id = ANY(%s))""", (ids, ids)).fetchall()


def set_embedding(conn, template_id: int, embedding) -> None:
    conn.execute("UPDATE template SET embedding = %s::vector WHERE id = %s",
                 (vec(embedding), template_id))


# =============================================================================
# Mappings
# =============================================================================
MAPPING_COLS = """id, template_id, version, slot_names, scope, emits, approved_by, confidence,
    source, active, evidence, created_at, registry_version"""


def active_mappings(conn) -> dict[int, dict]:
    return {r["template_id"]: r for r in conn.execute(
        f"SELECT {MAPPING_COLS} FROM mapping WHERE active")}


def mappings_for(conn, template_id: int) -> list[dict]:
    return conn.execute(f"SELECT {MAPPING_COLS} FROM mapping WHERE template_id = %s "
                        "ORDER BY version DESC", (template_id,)).fetchall()


def latest_suggestion(conn, template_id: int) -> dict | None:
    return conn.execute(
        f"""SELECT {MAPPING_COLS} FROM mapping WHERE template_id = %s AND source = 'model'
            ORDER BY version DESC LIMIT 1""", (template_id,)).fetchone()


def _is_ignore(m: dict) -> bool:
    return (m.get("evidence") or {}).get("decision") == "ignored"


def save_mapping(conn, template_id: int, *, emits, scope=None, slot_names=(), source: str,
                 approved_by=None, confidence=None, active=False, evidence=None,
                 model_version: str | None = None) -> dict:
    """New mapping version. Active -> template APPROVED; model suggestion -> SUGGESTED.
    G2: source='auto_ml' marks auto-approved mappings (active=True set by caller).
    G4: model_version tags the suggestion for rollback tracking (§12.9).
    A decision (active mapping or ignore) bumps the registry version; a model suggestion
    changes no result, so it is stamped with the current version.
    -> {id, version, registry_version}"""
    status = conn.execute("SELECT status FROM template WHERE id = %s FOR UPDATE",
                          (template_id,)).fetchone()["status"]
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM mapping "
                           "WHERE template_id = %s", (template_id,)).fetchone()["v"]
    if active or _is_ignore({"evidence": evidence}):
        rv = bump(conn, "mapping", f"template:{template_id}", approved_by,
                  {"action": "approve" if active else "ignore", "version": version,
                   "source": source})
    else:
        rv = current_version(conn)
    if active:
        conn.execute("UPDATE mapping SET active = false WHERE template_id = %s AND active",
                     (template_id,))
    mid = conn.execute(
        """INSERT INTO mapping (template_id, version, slot_names, scope, emits, approved_by,
                                confidence, source, active, evidence, registry_version,
                                model_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (template_id, version, Jsonb(list(slot_names)), Jsonb(scope) if scope else None,
         Jsonb(emits), approved_by, confidence, source, active,
         Jsonb(evidence) if evidence is not None else None, rv,
         model_version)).fetchone()["id"]
    if active:
        new_status = "approved"
    elif source in ("model", "auto_ml") and status in ("unknown", "suggested"):
        new_status = "suggested" if source == "model" else "approved"
    else:
        new_status = status
    conn.execute("UPDATE template SET status = %s WHERE id = %s", (new_status, template_id))
    return {"id": mid, "version": version, "registry_version": rv}


def save_training_label(conn, template_id: int, *, suggested_key: str | None,
                        chosen_key: str | None, decision: str, candidates: list | None = None,
                        model_version: str | None = None) -> int:
    """Record a reviewer decision for reranker training (design §13.6 step 4, G4).
    Written on every approve / edited-approve / ignore / new_key action.
    -> label row id"""
    return conn.execute(
        """INSERT INTO training_label (template_id, suggested_key, chosen_key, decision,
                                       candidates, model_version)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (template_id, suggested_key, chosen_key, decision,
         Jsonb(candidates) if candidates else None, model_version)).fetchone()["id"]


def auto_approved_mappings(conn, limit: int = 100) -> list[dict]:
    """Audit list of auto-ML-approved mappings for spot checks (design §12.7, G2)."""
    return conn.execute(
        f"""SELECT {MAPPING_COLS}, t.display, t.vendor_family
            FROM mapping m JOIN template t ON t.id = m.template_id
            WHERE m.source = 'auto_ml' AND m.active
            ORDER BY m.created_at DESC LIMIT %s""", (limit,)).fetchall()


def ignore_template(conn, template_id: int, user: str | None, source: str = "human") -> int:
    """IGNORED is a decision: recorded as an inactive, empty mapping version for the audit
    trail. -> registry version"""
    saved = save_mapping(conn, template_id, emits=[], source=source, approved_by=user,
                         evidence={"decision": "ignored"})
    conn.execute("UPDATE mapping SET active = false WHERE template_id = %s", (template_id,))
    conn.execute("UPDATE template SET status = 'ignored' WHERE id = %s", (template_id,))
    return saved["registry_version"]


def activate_version(conn, template_id: int, version: int, user: str | None) -> int:
    """Rollback / roll forward: make an earlier reviewed version the active one (an ignore
    version re-ignores the template). -> registry version"""
    m = conn.execute(f"SELECT {MAPPING_COLS} FROM mapping WHERE template_id = %s AND version = %s",
                     (template_id, version)).fetchone()
    if m is None:
        raise LookupError(f"template {template_id} has no mapping version {version}")
    if m["source"] == "model":
        raise ValueError("a model suggestion is not a reviewed mapping: approve it instead")
    conn.execute("SELECT id FROM template WHERE id = %s FOR UPDATE", (template_id,))
    conn.execute("UPDATE mapping SET active = false WHERE template_id = %s AND active",
                 (template_id,))
    if _is_ignore(m):
        status = "ignored"
    else:
        conn.execute("UPDATE mapping SET active = true WHERE id = %s", (m["id"],))
        status = "approved"
    conn.execute("UPDATE template SET status = %s WHERE id = %s", (status, template_id))
    return bump(conn, "mapping", f"template:{template_id}", user,
                {"action": "activate", "version": version})


def reset_to_unknown(conn, template_id: int, user: str | None) -> int:
    """Back to UNKNOWN (design 9.4): the active mapping is deactivated, history is kept.
    -> registry version"""
    conn.execute("SELECT id FROM template WHERE id = %s FOR UPDATE", (template_id,))
    conn.execute("UPDATE mapping SET active = false WHERE template_id = %s AND active",
                 (template_id,))
    conn.execute("UPDATE template SET status = 'unknown' WHERE id = %s", (template_id,))
    return bump(conn, "mapping", f"template:{template_id}", user, {"action": "reset"})


def mark_merged(conn, old_id: int, new_id: int) -> bool:
    """Offline consolidation (task E3) found a better-mined template for this signature: the
    old, never-reviewed one is retired (excluded from the review queue) but kept for
    traceability. Reviewed templates are never touched. Templates are not registry
    knowledge, so this does not bump the registry version (see upsert_template)."""
    row = conn.execute("SELECT status FROM template WHERE id = %s FOR UPDATE",
                       (old_id,)).fetchone()
    if row is None or row["status"] != "unknown":
        return False
    conn.execute("UPDATE template SET status = 'merged', merged_into = %s WHERE id = %s",
                (new_id, old_id))
    return True


def templates_by_family(conn, family: str, status: str | None = None) -> list[dict]:
    """All_templates, filtered, with 'parent' aliased from the join like Normalizer.reload
    (so the rows can go straight into mapping.compiler.build_index)."""
    out = []
    for t in all_templates(conn):
        if t["vendor_family"] != family or (status and t["status"] != status):
            continue
        t = dict(t)
        t["parent"] = t.pop("parent_signature")
        out.append(t)
    return out


def history(conn, template_id: int) -> dict:
    """{mappings: every version (newest first), changes: registry versions that touched it}"""
    return {"mappings": mappings_for(conn, template_id),
            "changes": changes(conn, f"template:{template_id}", limit=1000)}


# =============================================================================
# Vendor defaults, rules, devices
# =============================================================================
def upsert_defaults(conn, rows: list[dict], actor: str | None = "seed") -> int | None:
    before = {(r["vendor_family"], r["key"], r["os_range"] or ""): r for r in conn.execute(
        "SELECT vendor_family, key, value, os_range, note FROM vendor_default")}
    changed = []
    for r in rows:
        old = before.get((r["vendor_family"], r["key"], r.get("os_range") or ""))
        if old and old["value"] == r["value"] and old["note"] == r.get("note"):
            continue
        conn.execute(
            """INSERT INTO vendor_default (vendor_family, key, value, os_range, note)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (vendor_family, key, COALESCE(os_range, ''))
               DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note""",
            (r["vendor_family"], r["key"], Jsonb(r["value"]), r.get("os_range"), r.get("note")))
        changed.append(f"default:{r['vendor_family']}:{r['key']}"
                       + (f":{r['os_range']}" if r.get("os_range") else ""))
    return _bump_batch(conn, "default", changed, actor)


def vendor_defaults(conn, family: str) -> list[dict]:
    return conn.execute("SELECT key, value, os_range, note FROM vendor_default "
                        "WHERE vendor_family = %s ORDER BY key", (family,)).fetchall()


def all_vendor_defaults(conn) -> dict[str, list[dict]]:
    """{vendor_family: rows} in insertion order."""
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT vendor_family, key, value, os_range, note FROM vendor_default "
                          "ORDER BY id"):
        out.setdefault(r["vendor_family"], []).append(r)
    return out


RULE_COMPARED = ("benchmark", "key", "operator", "expected", "scope_selector", "severity",
                 "title", "ref")


def _rule_row(r: dict) -> dict:
    return {"id": r["id"], "benchmark": r["benchmark"], "key": r["key"],
            "operator": r["operator"], "expected": r.get("expected"),
            "scope_selector": r.get("scope_selector") or None,
            "severity": r.get("severity", "medium"), "title": r.get("title"),
            "ref": r.get("ref"), "seed_group": r.get("seed_group")}


def upsert_rules(conn, rows: list[dict], actor: str | None = "seed") -> int | None:
    before = {r["id"]: r for r in rules(conn)}
    changed = []
    for r in map(_rule_row, rows):
        old = before.get(r["id"])
        if old is None or any(old[f] != r[f] for f in RULE_COMPARED):
            changed.append(f"rule:{r['id']}")
        conn.execute(
            """INSERT INTO rule (id, benchmark, key, operator, expected, scope_selector,
                                 severity, title, ref, seed_group)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET benchmark = EXCLUDED.benchmark, key = EXCLUDED.key,
                   operator = EXCLUDED.operator, expected = EXCLUDED.expected,
                   scope_selector = EXCLUDED.scope_selector, severity = EXCLUDED.severity,
                   title = EXCLUDED.title, ref = EXCLUDED.ref, seed_group = EXCLUDED.seed_group""",
            (r["id"], r["benchmark"], r["key"], r["operator"],
             Jsonb(r["expected"]) if r["expected"] is not None else None,
             Jsonb(r["scope_selector"]) if r["scope_selector"] else None,
             r["severity"], r["title"], r["ref"], r["seed_group"]))
    return _bump_batch(conn, "rule", changed, actor)


def rules(conn, benchmark: str | None = None) -> list[dict]:
    return conn.execute("SELECT * FROM rule WHERE %s::text IS NULL OR benchmark = %s ORDER BY id",
                        (benchmark, benchmark)).fetchall()


# =============================================================================
# Jobs (design 5.2, 18.1) - not registry knowledge, so these never bump the version
# =============================================================================
JOB_STATUSES = ("RECEIVED", "DETECTED", "FLATTENED", "NORMALIZED", "PARTIALLY_NORMALIZED",
               "AUDITED", "NEEDS_VENDOR", "UNKNOWN_VENDOR", "FAILED")
# a job in one of these states holds a finished, reusable parse of its exact bytes
JOB_DONE_STATUSES = ("NORMALIZED", "PARTIALLY_NORMALIZED", "AUDITED")


def create_job(conn, job_id: str, device: str, object_key: str, sha256: str,
              uploader: str | None = None, declared_vendor: str | None = None) -> dict:
    return conn.execute(
        """INSERT INTO job (id, device, object_key, sha256, uploader, declared_vendor,
                            status_history)
           VALUES (%s, %s, %s, %s, %s, %s,
                   jsonb_build_array(jsonb_build_object('status', 'RECEIVED', 'at', now())))
           RETURNING *""",
        (job_id, device, object_key, sha256, uploader, declared_vendor)).fetchone()


def get_job(conn, job_id: str) -> dict | None:
    return conn.execute("SELECT * FROM job WHERE id = %s", (job_id,)).fetchone()


def find_reusable_job(conn, device: str, sha256: str) -> dict | None:
    """The latest job that finished normalizing this exact (device, content): a re-upload
    of unchanged bytes reuses it instead of re-parsing (design 5.2 idempotent re-upload)."""
    return conn.execute(
        """SELECT * FROM job WHERE device = %s AND sha256 = %s AND status = ANY(%s)
           ORDER BY created_at DESC LIMIT 1""",
        (device, sha256, list(JOB_DONE_STATUSES))).fetchone()


def set_job_detection(conn, job_id: str, vendor_family: str, dialect: str, detection: dict) -> None:
    conn.execute("UPDATE job SET vendor_family = %s, dialect = %s, detection = %s WHERE id = %s",
                (vendor_family, dialect, Jsonb(detection), job_id))


def update_job_status(conn, job_id: str, status: str, detail: dict | None = None) -> dict:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status {status!r}")
    row = conn.execute(
        """UPDATE job SET status = %s, updated_at = now(),
                          status_history = status_history || jsonb_build_array(
                              jsonb_build_object('status', %s::text, 'at', now(),
                                                  'detail', %s::jsonb))
           WHERE id = %s RETURNING *""",
        (status, status, Jsonb(detail), job_id)).fetchone()
    if row is None:
        raise ValueError(f"unknown job {job_id!r}")
    return row


def list_jobs(conn, device: str | None = None, status: str | None = None,
             limit: int = 50) -> list[dict]:
    return conn.execute(
        """SELECT * FROM job WHERE (%s::text IS NULL OR device = %s)
                            AND (%s::text IS NULL OR status = %s)
           ORDER BY created_at DESC LIMIT %s""",
        (device, device, status, status, limit)).fetchall()


def get_device_by_job(conn, job_id: str) -> dict | None:
    """The normalized document saved for a job, for reporting a reused job's earlier result."""
    return conn.execute("SELECT * FROM device WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                        (job_id,)).fetchone()


def save_device(conn, doc: dict, job_id: str | None = None) -> int:
    """One normalized config: observations, the templates it used (job_template) and the
    refreshed device_count of those templates. job_id (task D1) links back to the
    ingestion job that produced it, when the config went through `ingest`."""
    did = conn.execute(
        """INSERT INTO device (name, vendor_family, source_file, summary, registry_version,
                               job_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (doc["device"], doc["vendor_family"], doc["file"], Jsonb(doc["summary"]),
         doc.get("registry_version"), job_id)).fetchone()["id"]
    counts = doc.get("template_counts") or {}
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO observation (device_id, key, scope_type, scope_id, value, source,
                                        template_id, mapping_id, src_line, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(did, o["key"], o["scope_type"], o["scope_id"], Jsonb(o["value"]), o["source"],
              o.get("template_id"), o.get("mapping_id"), o.get("line"), o.get("raw"))
             for o in doc["values"]])
        cur.executemany("INSERT INTO job_template (job_id, template_id, count) VALUES (%s, %s, %s)",
                        [(did, int(tid), n) for tid, n in counts.items()])
    conn.execute(
        """UPDATE template t SET device_count = s.n
           FROM (SELECT jt.template_id, count(DISTINCT d.name) AS n
                 FROM job_template jt JOIN device d ON d.id = jt.job_id
                 WHERE jt.template_id = ANY(%s) GROUP BY jt.template_id) s
           WHERE t.id = s.template_id""", ([int(t) for t in counts],))
    return did


# =============================================================================
# Flat records (design 6.7, 13.6): store a job's parsed FlatCommand tree so
# re-normalization (and offline consolidation, task E3) never re-parses the raw upload.
# =============================================================================
def save_flat_records(conn, job_id: str, commands) -> None:
    conn.execute("DELETE FROM flat_record WHERE job_id = %s", (job_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO flat_record (job_id, seq, parent_seq, tokens, negated, is_block,
                                        inactive, secret_masked, src_line, src_lines, blob,
                                        raw, device_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(job_id, c.id, c.parent_id, Jsonb(c.tokens), c.negated, c.is_block,
              c.inactive, c.secret_masked, c.src_line,
              Jsonb(c.src_lines) if c.src_lines else None, c.blob, c.raw, c.device_id)
             for c in commands])


def _flat_command_tree(rows: list[dict]):
    """flat_record rows (one job, ordered by seq) -> [FlatCommand], parent-linked."""
    from app.core.models import FlatCommand
    by_seq: dict[int, FlatCommand] = {}
    out = []
    for r in rows:
        parent = by_seq.get(r["parent_seq"]) if r["parent_seq"] is not None else None
        c = FlatCommand(list(r["tokens"]), r["src_line"], parent, r["is_block"], r["raw"] or "",
                        negated=r["negated"], device_id=r["device_id"],
                        src_lines=list(r["src_lines"]) if r["src_lines"] else None,
                        blob=r["blob"], secret_masked=r["secret_masked"], inactive=r["inactive"])
        c.id, c.parent_id = r["seq"], r["parent_seq"]
        c.context = (parent.context + [list(parent.tokens)]) if parent is not None else []
        by_seq[r["seq"]] = c
        out.append(c)
    return out


def load_flat_records(conn, job_id: str) -> list:
    rows = conn.execute("SELECT * FROM flat_record WHERE job_id = %s ORDER BY seq",
                        (job_id,)).fetchall()
    return _flat_command_tree(rows)


def flat_records_for_family(conn, family: str) -> list[tuple[str, str, list]]:
    """Every stored (job_id, dialect, [FlatCommand]) of jobs detected as this vendor family."""
    jobs = conn.execute("SELECT id, dialect FROM job WHERE vendor_family = %s", (family,)).fetchall()
    out = []
    for j in jobs:
        rows = conn.execute("SELECT * FROM flat_record WHERE job_id = %s ORDER BY seq",
                            (j["id"],)).fetchall()
        if rows:
            out.append((j["id"], j["dialect"], _flat_command_tree(rows)))
    return out
