"""
seed_loader.py  -  bootstrap a registry from registry-seed/ (design section 4.2)

registry-seed/
  ontology/*.yaml                  canonical keys, one file per category
  vendors/<vendor>/profile.yaml    vendor profile row: dialect, dialect_params, fingerprints,
                                   parent (see ingest/fingerprints.py)
  vendors/<vendor>/mappings.yaml   reviewer-approved mappings for that vendor
  vendors/<vendor>/defaults.yaml   what absence of a command means for that vendor
  rules/*.yaml                     compliance rules
  learned/templates.json           offline mining output (`learn`), loaded by import_templates

The folder name is the vendor id: profile, mapping and default rows get it. Ontology and
rule rows remember their file (seed_group) so exporter.py writes them back to it.
After seeding, PostgreSQL is the source of truth; exporter.py writes it back to this layout.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from app import settings
from app.db.session import apply_schema
from app.ingest.fingerprints import load_profiles, validate_profile
from app.mapping.normalizer import parse_named_template, validate
from app.registry import repository as registry


def load_yaml(path: Path) -> list:
    return yaml.safe_load(Path(path).read_text()) or []


def _rows(seed_dir, pattern: str, per_vendor: bool = False, group: bool = False) -> list[dict]:
    out = []
    for path in sorted(Path(seed_dir or settings.SEED_DIR).glob(pattern)):
        rows = load_yaml(path)
        if per_vendor:
            rows = [{"vendor_family": path.parent.name, **r} for r in rows]
        if group:
            rows = [{**r, "seed_group": path.stem} for r in rows]
        out += rows
    return out


def ontology(seed_dir=None) -> list[dict]:
    return _rows(seed_dir, "ontology/*.yaml", group=True)


def profiles(seed_dir=None) -> list[dict]:
    rows = load_profiles(seed_dir=Path(seed_dir or settings.SEED_DIR))
    errors = [f"profile {r['name']}: {e}" for r in rows for e in validate_profile(r)]
    if errors:
        raise ValueError("; ".join(errors))
    return rows


def vendor_defaults(seed_dir=None) -> list[dict]:
    return _rows(seed_dir, "vendors/*/defaults.yaml", per_vendor=True)


def rules(seed_dir=None) -> list[dict]:
    return _rows(seed_dir, "rules/*.yaml", group=True)


def mappings(seed_dir=None) -> list[dict]:
    return _rows(seed_dir, "vendors/*/mappings.yaml", per_vendor=True)


def init(conn, seed_dir=None) -> dict:
    """Schema (Alembic upgrade) + vendor profiles + ontology (with embeddings) + vendor
    defaults + rules. Unchanged rows do not bump the registry version."""
    from ml.service.embed import Embedder, key_text     # until the ML service serves /embed
    apply_schema(conn)
    profile_rows = profiles(seed_dir)
    registry.upsert_profiles(conn, profile_rows)
    keys = ontology(seed_dir)
    registry.upsert_keys(conn, keys, Embedder().encode([key_text(k) for k in keys]))
    defaults = vendor_defaults(seed_dir)
    registry.upsert_defaults(conn, defaults)
    rule_rows = rules(seed_dir)
    registry.upsert_rules(conn, rule_rows)
    return {"profiles": len(profile_rows), "keys": len(keys), "defaults": len(defaults),
            "rules": len(rule_rows), "registry_version": registry.current_version(conn)}


def import_templates(conn, path) -> dict:
    """Templates (and the learned routing signature of each profile) from a `learn`
    templates.json."""
    data = json.loads(Path(path).read_text())
    for name, p in data["profiles"].items():
        registry.record_learned(conn, name, p["dialect"], p["signature"], p["files"])
    n = import_template_data(conn, data["templates"])
    return {"templates": n["templates"], "new": n["new"], "profiles": len(data["profiles"])}


def import_template_data(conn, templates: list[dict]) -> dict:
    """The upsert half of import_templates, also used by offline consolidation (task E3),
    which mines templates from stored flat records rather than a templates.json file."""
    from ml.service.embed import Embedder, template_text
    templates = sorted(templates, key=lambda t: len(t["path"]))
    rows = registry.existing_signatures(conn)
    todo = [t for t in templates if t["id"] not in rows or not rows[t["id"]]["has_embedding"]]
    vectors = dict(zip((t["id"] for t in todo),
                       Embedder().encode([template_text(t["path"]) for t in todo]) if todo else []))
    new = 0
    for t in templates:
        rec = {"signature": t["id"], "vendor_family": t["vendor_family"], "dialect": t["dialect"],
               "pattern": t["pattern"], "path": t["path"], "structured": t["structured"],
               "slots": t["slots"], "examples": t["examples"], "frequency": t["lines"]}
        parent = rows.get(t["parent"]) if t["parent"] else None
        row = registry.upsert_template(conn, rec, parent, vectors.get(t["id"]))
        rows[t["id"]] = row
        new += row["inserted"]
    return {"templates": len(templates), "new": new, "rows": rows}


def api_seed(conn, seed_dir=None) -> dict:
    """Full registry bootstrap without ML embeddings, for the FastAPI lifespan.

    Steps:
      1. Schema (idempotent CREATE IF NOT EXISTS)
      2. Vendor profiles
      3. Canonical-key ontology  (embeddings stay NULL; ML backfills later)
      4. Vendor defaults
      5. Compliance rules
      6. Pre-mined templates from learned/templates.json  (embeddings NULL)
      7. Reviewer-approved seed mappings from vendors/*/mappings.yaml
    """
    apply_schema(conn)

    profile_rows = profiles(seed_dir)
    registry.upsert_profiles(conn, profile_rows)

    key_rows = ontology(seed_dir)
    registry.upsert_keys(conn, key_rows, [None] * len(key_rows))

    default_rows = vendor_defaults(seed_dir)
    registry.upsert_defaults(conn, default_rows)

    rule_rows = rules(seed_dir)
    registry.upsert_rules(conn, rule_rows)

    tpl_result = _import_templates_no_ml(conn, seed_dir)
    mapping_result = seed_mappings(conn, seed_dir)

    return {
        "profiles": len(profile_rows),
        "keys": len(key_rows),
        "defaults": len(default_rows),
        "rules": len(rule_rows),
        "templates": tpl_result,
        "mappings": mapping_result,
        "registry_version": registry.current_version(conn),
    }


def _import_templates_no_ml(conn, seed_dir=None) -> dict:
    """Import learned/templates.json without ML embeddings (vectors stay NULL)."""
    tpl_path = Path(seed_dir or settings.SEED_DIR) / "learned" / "templates.json"
    if not tpl_path.exists():
        return {"skipped": "templates.json not found"}

    data = json.loads(tpl_path.read_text())
    for name, p in data["profiles"].items():
        registry.record_learned(conn, name, p["dialect"], p["signature"], p["files"])

    # parents must be inserted before children — sort by path depth
    templates = sorted(data["templates"], key=lambda t: len(t["path"]))
    rows = registry.existing_signatures(conn)
    new = 0
    for t in templates:
        rec = {
            "signature": t["id"],
            "vendor_family": t["vendor_family"],
            "dialect": t["dialect"],
            "pattern": t["pattern"],
            "path": t["path"],
            "structured": t["structured"],
            "slots": t["slots"],
            "examples": t["examples"],
            "frequency": t["lines"],
        }
        parent = rows.get(t["parent"]) if t.get("parent") else None
        row = registry.upsert_template(conn, rec, parent, embedding=None)
        rows[t["id"]] = row
        new += row["inserted"]

    return {"total": len(templates), "new": new, "profiles": len(data["profiles"])}


def seed_mappings(conn, seed_dir=None) -> dict:
    """Reviewer-approved mappings and ignores. Each is validated on the template's examples;
    unchanged ones are skipped. -> counts + warnings"""
    keys = registry.keys(conn)
    active = registry.active_mappings(conn)
    done = skipped = 0
    warnings = []
    for entry in mappings(seed_dir):
        display, names = parse_named_template(entry["template"])
        t = registry.get_template(conn, display, entry["vendor_family"])
        if t is None:
            warnings.append(f"not found: [{entry['vendor_family']}] {display}")
            continue
        if entry.get("ignore"):
            if t["status"] != "ignored":
                registry.ignore_template(conn, t["id"], "seed", source="seed")
                done += 1
            else:
                skipped += 1
            continue
        m = {"emits": entry.get("emits") or [], "scope": entry.get("scope"), "slot_names": names}
        errors, _ = validate(m, t, keys)
        if errors:
            warnings.append(f"{display}: " + "; ".join(errors))
            continue
        cur = active.get(t["id"])
        if cur and all(cur[k] == m[k] for k in ("emits", "scope", "slot_names")):
            skipped += 1
            continue
        registry.save_mapping(conn, t["id"], **m, source="seed", approved_by="seed", active=True)
        done += 1
    return {"done": done, "skipped": skipped, "warnings": warnings}
