"""
mapper  -  mapping model CLI: templates -> canonical keys, SIH 26155

  docker-compose up -d                                 # or pgserver, see Makefile
  python3 -m app.cli.mapper init                       # migrations + profiles, ontology, defaults, rules
  python3 -m app.cli.mapper import registry-seed/learned/templates.json
  python3 -m app.cli.mapper seed                       # reviewer-approved seed mappings
  python3 -m app.cli.mapper suggest --family arista    # model suggestions for unknown templates
  python3 -m app.cli.mapper review --family arista     # human-in-the-loop review
  python3 -m app.cli.mapper approve 123 --candidate 1  # or --key K --value EXPR [--scope-type ..]
  python3 -m app.cli.mapper ignore 123
  python3 -m app.cli.mapper history 123                # mapping versions + registry changes
  python3 -m app.cli.mapper activate 123 2             # roll back / forward to version 2
  python3 -m app.cli.mapper reset 123                  # back to UNKNOWN
  python3 -m app.cli.mapper version                    # registry version + recent changes
  python3 -m app.cli.mapper export --out /tmp/registry # registry -> registry-seed layout
  python3 -m app.cli.mapper normalize new_device.cfg [--save] [--json out.json]
  python3 -m app.cli.mapper check new_device.cfg       # normalise + phase-1 rules
  python3 -m app.cli.mapper ingest new_device.cfg --device rtr-01   # job pipeline (5.2, 5.4)
  python3 -m app.cli.mapper consolidate junos          # re-mine stored records (7.7)

Run from the repo root with PYTHONPATH=backend:. (`make seed` does init + import + seed).
DATABASE_URL selects the database; SIH_EMBEDDER=hash uses an offline embedder.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app import settings
from app.compliance.evaluator import evaluate
from app.compliance.rule_loader import load_rules
from app.db.session import connect
from app.mapping.normalizer import Normalizer, named_display, validate
from app.registry import exporter, seed_loader
from app.registry import repository as registry
from app.reporting.builder import text_report


def _short(v, n=60) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _template(conn, ref, family=None) -> dict:
    t = registry.get_template(conn, ref, family)
    if t is None:
        sys.exit(f"template {ref!r} not found")
    return t


# =============================================================================
# Setup
# =============================================================================
def cmd_init(args, conn):
    n = seed_loader.init(conn, args.seed)
    conn.commit()
    print(f"schema ok; {n['profiles']} vendor profiles, {n['keys']} canonical keys, "
          f"{n['defaults']} vendor defaults, {n['rules']} rules; "
          f"registry version {n['registry_version']}")


def cmd_import(args, conn):
    n = seed_loader.import_templates(conn, args.templates)
    conn.commit()
    print(f"{n['templates']} templates ({n['new']} new) from {n['profiles']} profiles")


def cmd_seed(args, conn):
    n = seed_loader.seed_mappings(conn, args.seed)
    conn.commit()
    for w in n["warnings"]:
        print(f"  ! {w}")
    print(f"seeded {n['done']} mappings/ignores ({n['skipped']} unchanged); "
          f"registry version {registry.current_version(conn)}")


def cmd_export(args, conn):
    n = exporter.export(conn, args.out)
    print(f"exported registry version {n['registry_version']} to {args.out}: "
          + ", ".join(f"{v} {k}" for k, v in n.items() if k != "registry_version"))


def cmd_version(args, conn):
    print(f"registry version {registry.current_version(conn)}")
    for c in registry.changes(conn, limit=args.limit):
        print(f"  v{c['version']:<6} {c['changed_at']:%Y-%m-%d %H:%M:%S}  {c['change_kind']:<8} "
              f"{c['actor'] or '':<10} {c['change_ref'] or ''}")


# =============================================================================
# Suggestions and review
# =============================================================================
def cmd_suggest(args, conn):
    from ml.service.rerank import Suggester
    s = Suggester(conn)
    statuses = ("unknown", "suggested") if args.refresh else ("unknown",)
    queue = registry.review_queue(conn, args.family, args.limit, statuses)
    s.ensure_embeddings([t["id"] for t in queue])
    for t in queue:
        best = s.suggest(t)
        conn.commit()
        if best:
            prev = f" -> {_short(best['preview'][0][2], 30)}" if best["preview"] else ""
            print(f"  #{t['id']:<6} {_short(t['display'], 64):<64} {best['key']:<28} "
                  f"{best['score']:.2f}{prev}")
    print(f"{len(queue)} template(s) suggested")


def _print_template(conn, t):
    print(f"\n#{t['id']}  [{t['vendor_family']}]  {t['display']}")
    print(f"    status={t['status']}  lines={t['frequency']}  devices={t['device_count']}  "
          f"signature={t['signature']}")
    if t["vars"]:
        print("    slots: " + "  ".join(f"{v['name']}=<{v['type']}> e.g. {_short(v['sample'], 25)!r}"
                                     for v in t["vars"]))
    for ex in t["examples"][:2]:
        print(f"    e.g. L{ex.get('line')}: {_short(ex.get('raw', ''), 90)}")


def _print_candidates(cands):
    for i, c in enumerate(cands, 1):
        p = c["proposal"] or {}
        emits = "; ".join(f"{e['key']} = {e['value']}" for e in p.get("emits") or []) or "-"
        scope = f"  scope={p['scope']['type']}:{p['scope']['id']}" if p.get("scope") else ""
        prev = ", ".join(f"{k}={_short(v, 30)}" for k, _, v in c["preview"])
        print(f"   {i}) {c['key']:<28} score={c['score']:.2f} (knn {c['knn']:.2f}, "
              f"key {c['zero_shot']:.2f}, fit {c['fit']:.2f})")
        if c["kind"] != "ignore":
            print(f"        {emits}{scope}" + (f"   -> {prev}" if prev else ""))
        for r in c["reasons"][:2]:
            print(f"        · {r}")
        for e in c["errors"]:
            print(f"        ! {e}")


def _ask(prompt, default=""):
    got = input(f"    {prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return got or default


def _edit(conn, t, keys, cand):
    """Prompt for a mapping, starting from a candidate. Returns a validated mapping or None."""
    p = (cand or {}).get("proposal") or {}
    emit = (p.get("emits") or [{}])[0]
    key = _ask("key", emit.get("key", ""))
    if key not in keys:
        print(f"    ! unknown key {key!r}")
        return None
    value = _ask("value expression", emit.get("value", ""))
    when = _ask("when (optional)", emit.get("when", ""))
    scope = p.get("scope") or {}
    stype = _ask("scope type", scope.get("type") or ("" if keys[key]["scope"] == "GLOBAL"
                                                    else keys[key]["scope"]))
    sid = _ask("scope id", scope.get("id", "")) if stype else ""
    names = _ask("slot names v1,v2.. (optional)", ",".join(p.get("slot_names") or []))
    m = {"emits": [{"key": key, "value": value, **({"when": when} if when else {})}],
         "scope": {"type": stype, "id": sid} if stype else None,
         "slot_names": [n.strip() for n in names.split(",")] if names else []}
    errors, preview = validate(m, t, keys)
    for e in errors:
        print(f"    ! {e}")
    if errors:
        return None
    print("    preview: " + ", ".join(f"{k}{'' if w[0] == 'GLOBAL' else f' [{w[0]}:{w[1]}]'} = {v!r}"
                                     for k, w, v, _ in preview))
    return m if _ask("save? y/n", "y").lower().startswith("y") else None


def cmd_review(args, conn):
    from ml.service.rerank import Suggester
    s = Suggester(conn)
    queue = registry.review_queue(conn, args.family, args.limit)
    print(f"{len(queue)} template(s) to review   (1-3 accept, e edit, i ignore, s skip, q quit)")
    for t in queue:
        s.ensure_embeddings([t["id"]])
        cands = s.candidates(t)                  # fresh: earlier decisions already count
        _print_template(conn, t)
        _print_candidates(cands)
        while True:
            choice = input("  > ").strip().lower()
            if choice == "q":
                return
            if choice in ("s", ""):
                break
            if choice == "i":
                registry.ignore_template(conn, t["id"], args.user)
                conn.commit()
                print("    ignored")
                break
            if choice.isdigit() and 1 <= int(choice) <= len(cands):
                c = cands[int(choice) - 1]
                if c["kind"] == "ignore":
                    registry.ignore_template(conn, t["id"], args.user)
                    print("    ignored")
                elif c["errors"]:
                    print("    ! candidate has errors, use e to edit")
                    continue
                else:
                    p = c["proposal"]
                    registry.save_mapping(conn, t["id"], emits=p["emits"], scope=p["scope"],
                                          slot_names=p["slot_names"], source="human",
                                          approved_by=args.user, confidence=c["score"], active=True)
                    print(f"    approved -> {c['key']}")
                conn.commit()
                break
            if choice == "e":
                m = _edit(conn, t, s.keys, cands[0] if cands else None)
                if m:
                    registry.save_mapping(conn, t["id"], **m, source="human",
                                          approved_by=args.user, active=True)
                    conn.commit()
                    print("    approved")
                    break
                continue
            print("    ? 1-3 / e / i / s / q")


def cmd_show(args, conn):
    t = _template(conn, args.template, args.family)
    _print_template(conn, t)
    _print_versions(t, registry.mappings_for(conn, t["id"]))
    s = registry.latest_suggestion(conn, t["id"])
    if s and s["evidence"]:
        print("    model candidates:")
        _print_candidates(s["evidence"].get("candidates", []))


def _print_versions(t, mappings):
    for m in mappings:
        flag = "ACTIVE" if m["active"] else "      "
        emits = "; ".join(f"{e['key']} = {e['value']}" for e in m["emits"]) or \
            (m["evidence"] or {}).get("decision", "-")
        print(f"    v{m['version']:<3} {flag} {m['source']:<6} {m['approved_by'] or '':<10} "
              f"r{m['registry_version']:<5} {emits}"
              + (f"  scope={m['scope']}" if m["scope"] else ""))
        if m["active"]:
            print(f"         {named_display(t, m['slot_names'])}")


def cmd_history(args, conn):
    t = _template(conn, args.template, args.family)
    h = registry.history(conn, t["id"])
    _print_template(conn, t)
    _print_versions(t, h["mappings"])
    print("    registry changes:")
    for c in h["changes"]:
        d = c["detail"] or {}
        what = d.get("action", "") + (f" v{d['version']}" if "version" in d else "")
        print(f"      r{c['version']:<6} {c['changed_at']:%Y-%m-%d %H:%M:%S}  "
              f"{c['actor'] or '':<10} {what}")


def cmd_activate(args, conn):
    t = _template(conn, args.template, args.family)
    m = next((m for m in registry.mappings_for(conn, t["id"]) if m["version"] == args.version),
             None)
    if m is None:
        sys.exit(f"#{t['id']} has no mapping version {args.version}")
    if m["emits"] or m["scope"]:
        errors, _ = validate(m, t, registry.keys(conn))
        if errors:
            sys.exit(f"v{args.version} fails on the current examples: " + "; ".join(errors))
    try:
        rv = registry.activate_version(conn, t["id"], args.version, args.user)
    except (LookupError, ValueError) as e:
        sys.exit(str(e))
    conn.commit()
    print(f"#{t['id']} {t['display']}: v{args.version} active (registry version {rv})")


def cmd_reset(args, conn):
    t = _template(conn, args.template, args.family)
    rv = registry.reset_to_unknown(conn, t["id"], args.user)
    conn.commit()
    print(f"#{t['id']} {t['display']}: back to unknown (registry version {rv})")


def cmd_approve(args, conn):
    t = _template(conn, args.template, args.family)
    keys = registry.keys(conn)
    if args.candidate:
        s = registry.latest_suggestion(conn, t["id"])
        cands = (s["evidence"] or {}).get("candidates", []) if s else []
        if not 1 <= args.candidate <= len(cands):
            sys.exit("no such candidate (run suggest first)")
        c = cands[args.candidate - 1]
        if c["kind"] == "ignore":
            registry.ignore_template(conn, t["id"], args.user)
            conn.commit()
            print("ignored")
            return
        m = {k: c["proposal"][k] for k in ("emits", "scope", "slot_names")}
    else:
        if not args.key and not args.scope_type:
            sys.exit("--candidate, or --key/--value (and/or --scope-type/--scope-id)")
        emits = []
        if args.key:
            emits.append({"key": args.key, "value": args.value or "v1",
                          **({"when": args.when} if args.when else {})})
        m = {"emits": emits,
             "scope": {"type": args.scope_type, "id": args.scope_id or ""} if args.scope_type else None,
             "slot_names": [n.strip() for n in args.names.split(",")] if args.names else []}
    errors, preview = validate(m, t, keys)
    if errors:
        sys.exit("rejected: " + "; ".join(errors))
    registry.save_mapping(conn, t["id"], **m, source="human", approved_by=args.user, active=True)
    conn.commit()
    print(f"approved #{t['id']} {t['display']} (registry version {registry.current_version(conn)})")
    for k, w, v, _ in preview:
        print(f"  preview {k}{'' if w[0] == 'GLOBAL' else f' [{w[0]}:{w[1]}]'} = {v!r}")


def cmd_ignore(args, conn):
    t = _template(conn, args.template, args.family)
    registry.ignore_template(conn, t["id"], args.user)
    conn.commit()
    print(f"ignored #{t['id']} {t['display']}")


def cmd_queue(args, conn):
    for t in registry.review_queue(conn, args.family, args.limit):
        print(f"  #{t['id']:<6} {t['status']:<10} x{t['frequency']:<4} [{t['vendor_family']}] "
              f"{_short(t['display'], 90)}")


# =============================================================================
# Normalise / check
# =============================================================================
def _suggest_new(conn, normalizer, doc) -> None:
    """Stand-in for template.unknown -> ML service. Optional: a failure only leaves the new
    templates without suggestions (they are UNKNOWN in the review queue either way)."""
    ids = doc["new_template_ids"]
    if not ids:
        return
    try:
        from ml.service.rerank import Suggester
        s = Suggester(conn)
        s.ensure_embeddings(ids)
        for tid in ids:
            s.suggest(registry.get_template(conn, str(tid)))
        conn.commit()
    except Exception as e:                  # ML is optional (design 4.1): never fail the run
        conn.rollback()
        print(f"  ! suggester unavailable ({type(e).__name__}: {e}); "
              f"{len(ids)} template(s) queued without suggestions", file=sys.stderr)
        return
    normalizer.refresh_pending(doc)


def _normalize_all(args, conn) -> list[dict]:
    n = Normalizer(conn)
    docs = []
    for path in args.paths:
        doc = n.run(path, learn=not args.no_learn, count=args.save)
        if args.save:
            doc["device_db_id"] = registry.save_device(conn, doc)
        conn.commit()
        if not args.no_suggest:
            _suggest_new(conn, n, doc)
        docs.append(doc)
    return docs


def _print_doc(doc, limit):
    s = doc["summary"]
    print(f"\n=== {doc['device']}   family={doc['vendor_family']}"
          f"{' (NEW)' if doc['new_vendor_family'] else ''}   dialect={doc['dialect']}   {doc['file']}")
    print("    " + "  ".join(f"{k}={v}" for k, v in s.items()))
    print("  VALUES")
    for o in doc["values"]:
        scope = "" if o["scope_type"] == "GLOBAL" else f" [{o['scope_type']}:{o['scope_id']}]"
        src = f"L{o['line']}  {_short(o['raw'], 40)}" if o["source"] == "config" else \
            f"(vendor default{': ' + o['note'] if o.get('note') else ''})"
        print(f"    {o['key'] + scope:<44} = {_short(o['value'], 34):<34} {src}")
    if doc["pending"]:
        print(f"  PENDING REVIEW ({len(doc['pending'])} templates, top {limit})")
        for p in doc["pending"][:limit]:
            sug = p["suggestion"]
            hint = ""
            if sug:
                prev = f" -> {_short(sug['preview'][0][2], 24)}" if sug["preview"] else ""
                hint = f"   suggest {sug['key']} ({sug['score']:.2f}){prev}"
            print(f"    #{p['template_id']:<6} x{len(p['lines']):<3} {_short(p['display'], 60):<60}{hint}")
    for c in doc["conflicts"][:limit]:
        print(f"  CONFLICT {c['key']} {c['scope']}: L{c['was_line']}={c['was']!r} vs L{c['line']}={c['now']!r}")
    for e in doc["errors"][:limit]:
        print(f"  ERROR {e}")


def _write_json(target, payload):
    text = json.dumps(payload, indent=1, default=str)
    if target == "-":
        print(text)
    else:
        Path(target).write_text(text)
        print(f"\nwrote {target}")


def cmd_normalize(args, conn):
    docs = _normalize_all(args, conn)
    if args.json != "-":
        for doc in docs:
            _print_doc(doc, args.show)
    if args.json:
        _write_json(args.json, docs)


def cmd_check(args, conn):
    docs = _normalize_all(args, conn)
    rules = load_rules(conn, args.benchmark)
    out = []
    for doc in docs:
        results = evaluate(doc, rules)
        if args.json != "-":
            print(f"\n    normalised: " + "  ".join(f"{k}={v}" for k, v in doc["summary"].items()))
            print(text_report(doc, results))
        out.append({"device": doc["device"], "file": doc["file"],
                    "vendor_family": doc["vendor_family"],
                    "registry_version": doc["registry_version"], "results": results})
    if args.json:
        _write_json(args.json, out)


# =============================================================================
# Ingestion (design 5.2, 5.4, 18.1): job lifecycle, raw storage, vendor ambiguity
# =============================================================================
def cmd_ingest(args, conn):
    from app.ingest import jobs
    from app.ingest.detector import detect_vendor

    data = Path(args.path).read_bytes()
    device = args.device or Path(args.path).stem
    object_key, digest = jobs.store_raw(device, data)

    reused = registry.find_reusable_job(conn, device, digest)
    if reused:
        seen = registry.get_device_by_job(conn, reused["id"])
        print(f"job {reused['id']}: unchanged since {reused['created_at']:%Y-%m-%d %H:%M:%S} "
              f"({reused['status']}); reusing it, nothing re-parsed")
        if seen:
            print("    " + "  ".join(f"{k}={v}" for k, v in seen["summary"].items()))
        return reused

    job = registry.create_job(conn, jobs.new_id(), device, object_key, digest,
                              args.uploader, args.declared_vendor)
    conn.commit()

    text = data.decode("utf-8", errors="replace")
    profiles = registry.vendor_profiles(conn)
    result = detect_vendor(text, profiles, args.declared_vendor, registry.profile_signatures(conn))
    registry.set_job_detection(conn, job["id"], result["profile"]["name"], result["dialect"], result)
    if result["mismatch"]:
        top = result["scores"][0]["name"] if result["scores"] else "generic"
        print(f"  ! declared vendor {args.declared_vendor!r} disagrees with the fingerprints "
              f"(best match: {top})")

    if result["status"] in ("NEEDS_VENDOR", "UNKNOWN_VENDOR"):
        registry.update_job_status(conn, job["id"], result["status"], detail=result)
        conn.commit()
        scores = ", ".join(f"{s['name']}={s['score']:.1f}" for s in result["scores"][:3])
        print(f"job {job['id']}: {result['status']}   device={device}"
              + (f"   ({scores})" if scores else ""))
        return registry.get_job(conn, job["id"])

    registry.update_job_status(conn, job["id"], "DETECTED")
    conn.commit()
    try:
        n = Normalizer(conn, profiles=profiles)
        doc = n.run(jobs.raw_path(device, digest), learn=not args.no_learn, count=True,
                   device=device, job_id=job["id"])
        registry.update_job_status(conn, job["id"], "FLATTENED")
        did = registry.save_device(conn, doc, job_id=job["id"])
        status = ("NORMALIZED" if not doc["pending"] and not doc["unrecognized"]
                 else "PARTIALLY_NORMALIZED")
        registry.update_job_status(conn, job["id"], status)
        conn.commit()
        if not args.no_suggest:
            _suggest_new(conn, n, doc)
        print(f"job {job['id']}: {status}   device_db_id={did}")
        if args.json != "-":
            _print_doc(doc, args.show)
        if args.json:
            _write_json(args.json, doc)
    except Exception as e:
        conn.rollback()
        registry.update_job_status(conn, job["id"], "FAILED", detail=str(e))
        conn.commit()
        raise
    return registry.get_job(conn, job["id"])


# =============================================================================
# Offline consolidation (design 7.7, 13.3): re-mine a vendor's stored flat records (task
# E2) and its sample corpus, so runtime-registered unknown templates merge the way an
# offline `learn` run would (one 'community <WORD>' instead of one template per literal
# community string), instead of staying split forever.
# =============================================================================
def cmd_consolidate(args, conn):
    from app.ingest.detector import detect
    from app.ingest.fingerprints import load_profiles
    from app.mapping.compiler import build_index
    from app.mapping.matcher import match_config
    from app.parsing.flattener import flatten_config
    from app.templating.hints import LearnerHints
    from app.templating.lexer import Lexer
    from app.templating.miner import TemplateLearner

    family = args.family
    lhints = LearnerHints.load(args.hints)
    profile_row = next((p for p in load_profiles() if p["name"] == family), None)

    files = []
    folder = settings.SAMPLES_DIR / family
    if folder.is_dir():
        profiles = registry.vendor_profiles(conn)
        for path in sorted(f for f in folder.rglob("*") if f.is_file()):
            text = path.read_text(encoding="utf-8", errors="replace")
            vendor, dialect = detect(text, profiles)
            files.append((str(path), dialect, flatten_config(text, dialect, vendor)))
    files += [(f"job:{jid}", dialect, cmds)
             for jid, dialect, cmds in registry.flat_records_for_family(conn, family)]

    if not files:
        print(f"no stored flat records or sample corpus for vendor {family!r}")
        return

    old_rows = registry.templates_by_family(conn, family)
    old_unknown = {t["signature"]: t for t in old_rows if t["status"] == "unknown"}

    learner = TemplateLearner(lhints)
    learner.learn_profile(family, files, profile_row=profile_row)
    mined = learner.to_json({family: {"dialect": files[0][1], "members": files, "sigs": []}}
                            )["templates"]
    n = seed_loader.import_template_data(conn, mined)
    conn.commit()

    # which old 'unknown' signatures does mining no longer produce, and what covers them now?
    lexer = Lexer(lhints.ifname_prefixes)
    old_index, new_index = build_index(old_rows), build_index(mined)
    mined_sigs = {t["id"] for t in mined}
    candidates: dict[str, Counter] = defaultdict(Counter)
    for _, _, cmds in files:
        old_hits = {c.id: t["signature"] for c, t, _, _ in match_config(cmds, old_index, [family], lexer) if t}
        new_hits = {c.id: t["id"] for c, t, _, _ in match_config(cmds, new_index, [family], lexer) if t}
        for cid, old_sig in old_hits.items():
            if old_sig in old_unknown and old_sig not in mined_sigs and cid in new_hits:
                candidates[old_sig][new_hits[cid]] += 1

    merged = 0
    for old_sig, counter in candidates.items():
        new_sig, _ = counter.most_common(1)[0]
        new_row = n["rows"].get(new_sig)
        if new_row and registry.mark_merged(conn, old_unknown[old_sig]["id"], new_row["id"]):
            merged += 1
    conn.commit()
    print(f"{family}: mined {len(mined)} templates ({n['new']} new) from "
         f"{sum(len(c) for _, _, c in files)} commands in {len(files)} file(s)/job(s); "
         f"{merged} old unknown template(s) merged")


# =============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Mapping model: templates -> canonical keys")
    ap.add_argument("--dsn", help="PostgreSQL DSN (default $DATABASE_URL)")
    ap.add_argument("--user", default="reviewer", help="name recorded as approved_by")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create schema, load ontology, defaults, rules")
    p.add_argument("--seed", default=str(settings.SEED_DIR), help="registry-seed folder")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="import templates.json from `learn`")
    p.add_argument("templates")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("seed", help="load reviewer-approved mappings from vendors/*/mappings.yaml")
    p.add_argument("--seed", default=str(settings.SEED_DIR), help="registry-seed folder")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("export", help="write the registry to the registry-seed layout")
    p.add_argument("--out", required=True, help="target folder")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("version", help="current registry version and recent changes")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_version)

    for name, fn, hlp in (("suggest", cmd_suggest, "model suggestions for unknown templates"),
                          ("review", cmd_review, "interactive human-in-the-loop review"),
                          ("queue", cmd_queue, "list templates waiting for review")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--family")
        p.add_argument("--limit", type=int, default=25)
        if name == "suggest":
            p.add_argument("--refresh", action="store_true", help="also redo SUGGESTED ones")
        p.set_defaults(func=fn)

    for name, fn in (("show", cmd_show), ("ignore", cmd_ignore), ("approve", cmd_approve),
                     ("history", cmd_history), ("activate", cmd_activate), ("reset", cmd_reset)):
        p = sub.add_parser(name, help=f"{name} a template (id, signature or display path)")
        p.add_argument("template")
        p.add_argument("--family", help="needed when TEMPLATE is a display path")
        if name == "activate":
            p.add_argument("version", type=int, help="mapping version to make active")
        if name == "approve":
            p.add_argument("--candidate", type=int, help="accept the model's candidate N")
            p.add_argument("--key")
            p.add_argument("--value", help="transform expression (default v1)")
            p.add_argument("--when", help="guard expression")
            p.add_argument("--scope-type")
            p.add_argument("--scope-id", help="format string, e.g. 'vty {v1} {v2}'")
            p.add_argument("--names", help="slot names for v1,v2,... e.g. ltype,lno,min,sec")
        p.set_defaults(func=fn)

    for name, fn in (("normalize", cmd_normalize), ("check", cmd_check)):
        p = sub.add_parser(name, help="normalise configs" + (" and evaluate rules" if name == "check" else ""))
        p.add_argument("paths", nargs="+")
        p.add_argument("--save", action="store_true", help="store device + observations")
        p.add_argument("--no-learn", action="store_true", help="do not add new templates")
        p.add_argument("--no-suggest", action="store_true", help="do not run the suggester")
        p.add_argument("--json", help="write JSON to a file, '-' for stdout")
        p.add_argument("--show", type=int, default=15, help="pending / error lines to print")
        if name == "check":
            p.add_argument("--benchmark")
        p.set_defaults(func=fn)

    p = sub.add_parser("ingest", help="upload one config through the job pipeline (design 5.2)")
    p.add_argument("path")
    p.add_argument("--device", help="default: the file name")
    p.add_argument("--uploader")
    p.add_argument("--declared-vendor", help="trust this vendor id; only warn on disagreement")
    p.add_argument("--no-learn", action="store_true", help="do not add new templates")
    p.add_argument("--no-suggest", action="store_true", help="do not run the suggester")
    p.add_argument("--json", help="write JSON to a file, '-' for stdout")
    p.add_argument("--show", type=int, default=15, help="pending / error lines to print")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("consolidate",
                       help="re-mine a vendor's stored flat records + sample corpus (design 7.7)")
    p.add_argument("family")
    p.add_argument("--hints", help="hints.json (learner knobs)")
    p.set_defaults(func=cmd_consolidate)

    args = ap.parse_args(argv)
    with connect(args.dsn) as conn:
        args.func(args, conn)


if __name__ == "__main__":
    main()
