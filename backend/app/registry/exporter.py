"""
exporter.py  -  write the registry back to the registry-seed/ layout (design 4.2)

  ontology/<seed_group>.yaml                       keys (seed_group: the file it came from,
                                                   else the key's category)
  vendors/<vendor>/profile.yaml                    reviewed profile fields (not `learned`)
  vendors/<vendor>/mappings.yaml                   active mappings (named slots) and ignores,
                                                   in decision order
  vendors/<vendor>/defaults.yaml                   vendor defaults
  rules/<seed_group>.yaml                          rules (default file: baseline)

Templates are not exported: they come from `learn` (learned/templates.json). A mapping
whose template exists only at runtime is exported too, but re-seeding it needs that
template in the target registry. Comments in hand-written seed files are not kept; the
round trip is exact on content: seed -> export -> seed -> export gives identical files.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.mapping.normalizer import named_display
from app.registry import repository as registry


def _dump(path: Path, rows, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, width=100,
                          default_flow_style=None) if rows else ""
    path.write_text(f"# {header}\n" + body)


def _num(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _key(k: dict) -> dict:
    out = {"key": k["key"], "type": k["type"]}
    if k["unit"]:
        out["unit"] = k["unit"]
    out["scope"] = k["scope"]
    if k["cardinality"] != "single":
        out["cardinality"] = k["cardinality"]
    out.update(category=k["category"], description=k["description"])
    if k["enum_values"]:
        out["enum_values"] = k["enum_values"]
    if k["aliases"]:
        out["aliases"] = k["aliases"]
    return out


def _profile(p: dict) -> dict:
    out = {"family": p["family"]}
    if p["parent"]:
        out["parent"] = p["parent"]
    out.update(dialect=p["dialect"], dialect_params=p["dialect_params"],
               fingerprints=[{"pattern": fp["pattern"], "weight": _num(fp.get("weight", 1))}
                             for fp in p["fingerprints"]],
               threshold=_num(p["threshold"]), status=p["status"])
    return out


def _rule(r: dict) -> dict:
    out = {"id": r["id"], "benchmark": r["benchmark"], "key": r["key"],
           "operator": r["operator"]}
    if r["expected"] is not None:
        out["expected"] = r["expected"]
    if r["scope_selector"]:
        out["scope_selector"] = r["scope_selector"]
    out["severity"] = r["severity"]
    for f in ("title", "ref"):
        if r[f] is not None:
            out[f] = r[f]
    return out


def _default(d: dict) -> dict:
    out = {"key": d["key"], "value": d["value"]}
    if d["os_range"]:
        out["os_range"] = d["os_range"]
    if d["note"]:
        out["note"] = d["note"]
    return out


def decisions(conn) -> list[dict]:
    """Every approved or ignored template with the mapping row that decided it, per vendor
    in decision order."""
    return conn.execute(
        f"""SELECT {registry.TEMPLATE_COLS}, m.id AS mapping_id, m.slot_names, m.scope, m.emits
            FROM template t LEFT JOIN template p ON p.id = t.parent_id
            JOIN LATERAL (SELECT * FROM mapping m
                          WHERE m.template_id = t.id
                            AND (m.active OR (t.status = 'ignored'
                                              AND m.evidence ->> 'decision' = 'ignored'))
                          ORDER BY m.active DESC, m.version DESC LIMIT 1) m ON true
            WHERE t.status IN ('approved', 'ignored')
            ORDER BY t.vendor_family, m.id""").fetchall()


def export(conn, out_dir) -> dict:
    out = Path(out_dir)
    version = registry.current_version(conn)
    counts = {"keys": 0, "profiles": 0, "mappings": 0, "defaults": 0, "rules": 0}

    groups: dict[str, list] = {}
    for k in registry.keys(conn).values():
        groups.setdefault(k["seed_group"] or k["category"], []).append(_key(k))
    for name, rows in sorted(groups.items()):
        _dump(out / "ontology" / f"{name}.yaml", rows, "Canonical keys (exported).")
        counts["keys"] += len(rows)

    vendors = out / "vendors"
    for p in registry.vendor_profiles(conn):
        _dump(vendors / p["name"] / "profile.yaml", _profile(p),
              f"Vendor profile {p['name']} (exported).")
        counts["profiles"] += 1

    by_vendor: dict[str, list] = {}
    for d in decisions(conn):
        entry = {"template": named_display(d, d["slot_names"] or [])}
        if d["status"] == "ignored":
            entry["ignore"] = True
        else:
            if d["scope"]:
                entry["scope"] = {"type": d["scope"]["type"], "id": d["scope"].get("id", "")}
            if d["emits"]:
                entry["emits"] = d["emits"]
        by_vendor.setdefault(d["vendor_family"], []).append(entry)
    for vendor, rows in sorted(by_vendor.items()):
        _dump(vendors / vendor / "mappings.yaml", rows, f"{vendor} mappings (exported).")
        counts["mappings"] += len(rows)

    for vendor, rows in sorted(registry.all_vendor_defaults(conn).items()):
        _dump(vendors / vendor / "defaults.yaml", [_default(d) for d in rows],
              f"{vendor} vendor defaults (exported).")
        counts["defaults"] += len(rows)

    rule_groups: dict[str, list] = {}
    for r in registry.rules(conn):
        rule_groups.setdefault(r["seed_group"] or "baseline", []).append(_rule(r))
    for name, rows in sorted(rule_groups.items()):
        _dump(out / "rules" / f"{name}.yaml", rows, "Compliance rules (exported).")
        counts["rules"] += len(rows)
    return {**counts, "registry_version": version}
