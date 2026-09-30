"""
normalizer.py  -  one device config -> canonical key/values

Mapping application (was apply.py). A mapping row:
    slot_names  ["ltype", "lno", "min", "sec"]         names for v1..vn of the path
    scope       {"type": "LINE", "id": "{ltype} {lno}"}
    emits       [{"key": "LINE.EXEC_TIMEOUT_SEC", "value": "min * 60 + sec", "when": "..."}]

Normalizer.run:
  1. detect (registry vendor profiles), flatten, route to a vendor family, match against
     the registry's templates of that vendor and its parent profiles (own ones win ties)
  2. matched + active mapping  -> run the transforms  -> values (with source line)
     matched + IGNORED          -> skipped
     matched, not yet reviewed  -> pending (with the model's suggestion, if any)
     unmatched                  -> typed-only template (no mining: that is offline, via
                                   `learn` + `mapper import`) -> new UNKNOWN template ->
                                   pending; returned in new_template_ids so the caller
                                   can ask the suggester (optional)
  3. keys still absent get the vendor default (scoped keys: per scope instance seen); a
     child profile's defaults override its parent's
The document carries the registry version the Normalizer loaded (reload()), so a result
can be traced to the exact knowledge it used.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from app.core.models import MAX_EXAMPLES
from app.ingest.detector import detect
from app.mapping import transforms as T
from app.mapping.defaults import apply_defaults
from app.mapping.matcher import match_config
from app.mapping.matcher_cache import MatcherCache
from app.parsing.base import DialectHints
from app.parsing.flattener import flatten_config
from app.registry import repository as registry
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer, typed_pattern
from app.templating.signature import (choose_profile, extract_values, file_signature, render,
                                      signature)

_SLOT = re.compile(r"<([A-Z0-9]+)(\.\.\.)?(?::(\w+))?>")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# list keys: union = add values not seen yet (default); append = keep order and duplicates
MERGE_POLICIES = ("union", "append")


# =============================================================================
# Mapping application and validation
# =============================================================================
def parse_named_template(text: str) -> tuple[str, list[str]]:
    """'line con <NUM:lno> > exec-timeout <NUM:min> <NUM>' -> (display, ['lno', 'min'])"""
    names: list[str] = []

    def strip(m):
        names.append(m.group(3) or "")
        return f"<{m.group(1)}{m.group(2) or ''}>"
    display = _SLOT.sub(strip, text.strip())
    while names and not names[-1]:
        names.pop()
    return display, names


def named_display(tpl: dict, slot_names) -> str:
    """Inverse of parse_named_template, for showing a mapping to a reviewer."""
    it = iter(list(slot_names) + [""] * len(tpl["vars"]))
    return _SLOT.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}"
                               f"{(':' + n) if (n := next(it)) else ''}>", tpl["display"])


def _reads_negated(emit: dict) -> bool:
    return "negated" in T.free_names(emit["value"]) or bool(
        emit.get("when") and "negated" in T.free_names(emit["when"]))


def apply_mapping(mapping: dict, tpl: dict, keys: dict, values, negated=False, tokens=(), raw="",
                  blob=None):
    """-> (scope (type, id) | None, [(key, (scope_type, scope_id), value, emit)], [errors])

    A negated command only emits through expressions that read 'negated':
    'no ip http server' -> not negated -> False; 'no logging host X' emits nothing."""
    types = [v["type"] for v in tpl["vars"]]
    names = T.variables(values, types, mapping.get("slot_names") or (), negated, tokens, raw, blob)
    errors: list[str] = []
    scope = None
    if mapping.get("scope"):
        try:
            scope = (mapping["scope"]["type"], T.render_scope(mapping["scope"].get("id", ""), names))
        except T.TransformError as e:
            return None, [], [f"scope: {e}"]
    out = []
    for emit in mapping.get("emits") or []:
        key = keys.get(emit["key"])
        try:
            if key is None:
                raise T.TransformError("unknown canonical key")
            if negated and not _reads_negated(emit):
                continue
            if emit.get("when") and not T.evaluate(emit["when"], names):
                continue
            value = T.coerce(T.evaluate(emit["value"], names), key)
            if key["scope"] == "GLOBAL":
                where = ("GLOBAL", "")
            elif scope and scope[0] == key["scope"]:
                where = scope
            else:
                raise T.TransformError(f"key needs a {key['scope']} scope")
            out.append((emit["key"], where, value, emit))
        except T.TransformError as e:
            errors.append(f"{emit['key']}: {e}")
    return scope, out, errors


def sample_inputs(tpl: dict) -> list[tuple[list, list[str], str, bool, int | None, str | None]]:
    """(values, tokens, raw, negated, line, blob) for every stored example. The template's own
    slots come from the example's tokens; parent slots use their sample value (examples keep no
    parent tokens). Examples stored before tokens were kept fall back to the slot samples."""
    depth = len(tpl["path"]) - 1
    samples = [str(v.get("sample", "")) for v in tpl["vars"]]
    parents = [s for s, v in zip(samples, tpl["vars"]) if v["level"] < depth]
    out = []
    for ex in tpl.get("examples") or []:
        raw = ex.get("raw", "")
        if ex.get("tokens"):
            values = parents + extract_values(ex["tokens"], tpl["pattern"])
            out.append((values, list(ex["tokens"]), raw, bool(ex.get("negated")), ex.get("line"),
                       ex.get("blob")))
        else:
            out.append((samples, raw.split(), raw, bool(ex.get("negated")), ex.get("line"),
                       ex.get("blob")))
    return out or [(samples, [], "", False, None, None)]


def validate(mapping: dict, tpl: dict, keys: dict) -> tuple[list[str], list]:
    """Static checks + a preview run on every stored example. -> (errors, preview emits)
    A mapping that fails on any example is rejected."""
    errors: list[str] = []
    n = len(tpl["vars"])
    names = [s for s in mapping.get("slot_names") or []]
    if len(names) > n:
        errors.append(f"{len(names)} slot names but the template has {n} slots")
    for s in names:
        if s and (not _IDENT.match(s) or s in T.RESERVED or re.fullmatch(r"v\d+", s)):
            errors.append(f"bad slot name {s!r}")
    if len([s for s in names if s]) != len({s for s in names if s}):
        errors.append("duplicate slot names")
    allowed = T.variable_names(n, names)

    scope = mapping.get("scope")
    if scope:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", scope.get("type", "")):
            errors.append(f"bad scope type {scope.get('type')!r}")
        unknown = T.scope_names(scope.get("id", "")) - allowed
        if unknown:
            errors.append(f"scope id uses unknown slot(s) {sorted(unknown)}")
    if not mapping.get("emits") and not scope:
        errors.append("mapping emits nothing and declares no scope (use ignore instead)")
    for emit in mapping.get("emits") or []:
        key = keys.get(emit.get("key"))
        if key is None:
            errors.append(f"unknown canonical key {emit.get('key')!r}")
            continue
        if "merge" in emit:
            if emit["merge"] not in MERGE_POLICIES:
                errors.append(f"{key['key']} merge must be one of {MERGE_POLICIES}")
            elif key["cardinality"] != "list":
                errors.append(f"{key['key']} is single-valued: merge applies to list keys")
        if key["scope"] != "GLOBAL" and (not scope or scope.get("type") != key["scope"]):
            errors.append(f"{key['key']} is {key['scope']}-scoped: mapping needs "
                          f"scope type {key['scope']}")
        for field in ("value", "when"):
            if field == "value" or emit.get(field):
                try:
                    T.check(emit.get(field, ""), allowed)
                except T.TransformError as e:
                    errors.append(f"{key['key']} {field}: {e}")
    if errors:
        return errors, []
    preview = []
    for values, tokens, raw, negated, line, blob in sample_inputs(tpl):
        _, emitted, run_errors = apply_mapping(mapping, tpl, keys, values, negated, tokens, raw,
                                              blob)
        preview += emitted
        where = f" L{line}" if line else ""
        errors += [f"preview{where}: {e}" for e in run_errors]
    return errors, preview


# =============================================================================
# Normalizer
# =============================================================================
def _slots(pattern, cmds) -> list[dict]:
    counters = defaultdict(Counter)
    for c in cmds:
        for i, v in enumerate(extract_values(c.tokens, pattern)):
            counters[i][v] += 1
    out = []
    for i, (kind, typ) in enumerate(el for el in pattern if el[0] != "kw"):
        s = {"type": typ, "list": kind == "list", "values": dict(counters[i].most_common(10))}
        if typ == "ENUM":
            s["enum"] = sorted(counters[i])
        out.append(s)
    return out


class Normalizer:
    def __init__(self, conn, fhints: DialectHints | None = None,
                 lhints: LearnerHints | None = None, profiles: list[dict] | None = None):
        """profiles: vendor profile rows to detect with (default: the registry's)."""
        self.conn = conn
        self.fhints = fhints or DialectHints()
        self.base_lhints = lhints or LearnerHints()
        self.given_profiles = profiles
        self.lhints = self.base_lhints
        self.lexer = Lexer(self.lhints.ifname_prefixes)
        self._cache = MatcherCache()
        self.reload()

    def reload(self) -> None:
        """Load the registry state every run() uses, and the version it corresponds to."""
        self.registry_version = registry.current_version(self.conn)
        self.templates = []
        for r in registry.all_templates(self.conn):
            t = dict(r)
            t.update(db_id=r["id"], id=r["signature"], parent=r["parent_signature"])
            self.templates.append(t)
        self.profiles = (self.given_profiles if self.given_profiles is not None
                         else registry.vendor_profiles(self.conn))
        self.sigs = registry.profile_signatures(self.conn)
        self.mappings = registry.active_mappings(self.conn)
        self.keys = registry.keys(self.conn)
        self.defaults = registry.all_vendor_defaults(self.conn)
        self.rules = registry.rules(self.conn)
        self._cache.invalidate_all()

    def _defaults(self, lineage: list[str]) -> list[dict]:
        """Vendor defaults along the profile lineage; the most specific profile wins."""
        rows: dict[tuple, dict] = {}
        for fam in reversed(lineage):
            for d in self.defaults.get(fam, []):
                rows[(d["key"], d["os_range"])] = d
        return list(rows.values())

    def _rule_keys(self) -> set[str]:
        """Set of canonical key names referenced by active rules."""
        return {r["key"] for r in self.rules}

    # ------------------------------------------------------------------ unknown templates
    @staticmethod
    def _example(path, c) -> dict:
        return {"file": str(path), "device": c.device_id or path.stem, "line": c.src_line,
                "raw": c.raw, "negated": c.negated, "tokens": c.tokens, "blob": c.blob}

    def _register_unknown(self, path, dialect, family, unmatched, tpl_of, vals_of) -> list[dict]:
        """Typed-only templates for unmatched commands, rooted under their (matched or newly
        registered) parent, upserted as UNKNOWN. Word slots are refined later by offline mining."""
        groups: dict[str, dict] = {}
        for c in unmatched:                             # config order: parents first
            parent = tpl_of.get(c.parent_id) if c.parent_id is not None else None
            if c.parent_id is not None and parent is None:
                continue
            pattern = typed_pattern(c.tokens, self.lexer.types(c.tokens, c.quoted, c.roles),
                                    c.roles, self.lhints)
            tpath = (list(parent["path"]) if parent else []) + [render(pattern)]
            sig = signature(family, dialect, tpath)
            g = groups.setdefault(sig, {
                "id": sig, "signature": sig, "vendor_family": family, "dialect": dialect,
                "path": tpath, "display": " > ".join(tpath), "pattern": pattern,
                "structured": c.roles is not None, "status": "unknown", "parent_ref": parent,
                "cmds": []})
            g["cmds"].append(c)
            g["is_block"] = g.get("is_block", False) or c.is_block
            tpl_of[c.id] = g
            vals_of[c.id] = extract_values(c.tokens, pattern)

        for g in groups.values():                       # parents were created first
            parent = g.pop("parent_ref")
            rec = {**g, "pattern": [list(el) for el in g["pattern"]],
                   "slots": _slots(g["pattern"], g["cmds"]), "frequency": len(g["cmds"]),
                   "examples": [self._example(path, c) for c in g["cmds"][:MAX_EXAMPLES]]}
            row = registry.upsert_template(
                self.conn, rec, {"id": parent["db_id"], "vars": parent["vars"]} if parent else None,
                add_frequency=True)
            g.update(db_id=row["id"], vars=row["vars"], status=row["status"],
                     slots=rec["slots"], examples=rec["examples"])
        return list(groups.values())

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _check_allowed(key: dict, value) -> str | None:
        """Return an error message if value violates key['allowed'], else None."""
        allowed = (key or {}).get("allowed")
        if not allowed:
            return None
        if "enum" in allowed:
            # For list values check each element; for scalar check directly.
            vals = value if isinstance(value, list) else [value]
            bad = [v for v in vals if v not in allowed["enum"]]
            if bad:
                return f"value {bad[0]!r} not in allowed {allowed['enum']}"
        if "min" in allowed and value is not None:
            try:
                if float(value) < float(allowed["min"]):
                    return f"value {value!r} below minimum {allowed['min']}"
            except (TypeError, ValueError):
                pass
        if "max" in allowed and value is not None:
            try:
                if float(value) > float(allowed["max"]):
                    return f"value {value!r} above maximum {allowed['max']}"
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _build_scopes_doc(obs: dict, pending_tpl_ids: list[int], coverage: dict) -> dict:
        """Convert the flat obs dict into the nested scopes shape (design §11.5)."""
        scopes: dict = {}
        for (key, stype, sid), entry in obs.items():
            cell: dict = {"value": entry["value"]}
            if entry.get("line") is not None:
                cell["src"] = [entry["line"]]
            else:
                cell["src"] = []
            if entry.get("template_id") is not None:
                cell["template"] = entry["template_id"]
            if entry.get("mapping_id") is not None:
                cell["mapping_v"] = entry["mapping_id"]
            if entry.get("default"):
                cell["default"] = True
            if stype == "GLOBAL":
                scopes.setdefault("GLOBAL", {})[key] = cell
            else:
                scopes.setdefault(stype, {}).setdefault(sid, {})[key] = cell
        return scopes

    # ------------------------------------------------------------------ main
    def run(self, path, learn: bool = True, count: bool = False, device: str | None = None,
           job_id: str | None = None) -> dict:
        """device: fallback name when the config has no detectable hostname (default:
        path.stem). Job ingestion passes the uploader's device name here, since a job's
        raw path is content-addressed (see app.ingest.jobs.raw_path), not a hostname.
        job_id: persist the flat records under this job (task E2), so a later re-audit
        (task H6) or offline consolidation (task E3) never re-parses the raw upload."""
        path = Path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        vendor, dialect = detect(text, self.profiles)
        cmds = flatten_config(text, dialect, vendor, self.fhints)
        if job_id:
            registry.save_flat_records(self.conn, job_id, cmds)
        lineage = registry.lineage(self.profiles, vendor["name"])
        if lineage:                                 # a registered vendor profile
            family = vendor["name"]
            new_family = not any(f in self.sigs for f in lineage)
        else:                                       # generic: route by top-level commands
            family = choose_profile(dialect, cmds, vendor["name"], self.sigs,
                                    self.lhints.cluster_threshold)
            new_family = family is None
            if new_family:
                family = vendor["name"] if vendor["name"] != "generic" else f"{dialect}-{path.stem}"
            lineage = registry.lineage(self.profiles, family) or [family]

        # design 7.2, 7.3: a vendor's own ifname_prefixes / value_after / reserved_keywords
        # win over the global defaults this Normalizer was built with.
        profile_row = next((p for p in self.profiles if p["name"] == family), None)
        self.lhints = LearnerHints.for_profile(profile_row, base=self.base_lhints)
        self.lexer = Lexer(self.lhints.ifname_prefixes)

        # F2: get per-family trie from cache; merge tries across the lineage so inherited
        # templates (parent vendor family) are also reachable via the combined index.
        index: dict = {}
        for fam in lineage:
            fam_index = self._cache.get_index(self.conn, fam, templates=self.templates)
            index.update(fam_index)

        tpl_of: dict[int, dict] = {}
        vals_of: dict[int, list] = {}
        unmatched = []
        for c, t, vals, _ in match_config(cmds, index, lineage, self.lexer):
            if t is None:
                unmatched.append(c)
            else:
                tpl_of[c.id], vals_of[c.id] = t, vals
        seen = Counter(t["db_id"] for t in tpl_of.values())
        first_use = {}
        for c in cmds:
            if c.id in tpl_of:
                first_use.setdefault(tpl_of[c.id]["db_id"], c)

        new = []
        if unmatched and learn:
            new = self._register_unknown(path, dialect, family, unmatched, tpl_of, vals_of)
            if new_family:
                registry.record_learned(self.conn, family, dialect,
                                        file_signature(dialect, cmds), [str(path)])
        if count:                                   # a recorded job: bookkeeping per template
            registry.bump_frequency(self.conn, seen)
            for tid, c in first_use.items():
                registry.add_examples(self.conn, tid, [self._example(path, c)])

        obs: dict[tuple, dict] = {}
        instances: dict[str, set] = defaultdict(set)
        full: dict[int, list] = {}
        pending: dict[int, list] = defaultdict(list)
        unrecognized, conflicts, errors = [], [], []
        counts = Counter()
        for c in cmds:
            t = tpl_of.get(c.id)
            if t is None:
                counts["unrecognized"] += 1
                unrecognized.append({"line": c.src_line, "path": c.path_str})
                continue
            pv = (full.get(c.parent_id, []) if c.parent_id is not None else []) + vals_of[c.id]
            full[c.id] = pv
            if t["status"] == "ignored":
                counts["ignored"] += 1
                continue
            m = self.mappings.get(t["db_id"])
            if m is None:
                counts["pending"] += 1
                pending[t["db_id"]].append(c)
                continue
            counts["mapped"] += 1
            scope, emitted, errs = apply_mapping(m, t, self.keys, pv, c.negated, c.tokens, c.raw,
                                                c.blob)
            errors += [f"L{c.src_line} {t['display']}: {e}" for e in errs]
            if scope:
                instances[scope[0]].add(scope[1])
            for key, (stype, sid), value, emit in emitted:
                # F6: validate value against key's allowed constraints
                key_def = self.keys.get(key)
                if key_def:
                    err = self._check_allowed(key_def, value)
                    if err:
                        errors.append(f"L{c.src_line} {key}: {err}")
                self._record(obs, conflicts, key, stype, sid, value, t, m, c,
                             emit.get("merge", "union"))

        # F5: apply defaults; rule_keys is available for callers who want to restrict defaults
        # to rule-referenced keys only, but we apply all defaults by default for backward compat.
        n_defaults = apply_defaults(obs, instances, self._defaults(lineage), self.keys)
        values = sorted(obs.values(), key=lambda o: (o["key"], o["scope_type"], o["scope_id"]))

        # F4: coverage metrics
        mapped_count = counts["mapped"]
        pending_count = counts["pending"]
        unrecognized_count = counts["unrecognized"]
        cov_denom = mapped_count + pending_count + unrecognized_count
        coverage = {
            "records": (mapped_count + pending_count) / cov_denom if cov_denom > 0 else 1.0,
            "rule_keys": None,
        }

        # F3: nested scopes document
        pending_tpl_ids = [g["db_id"] for g in new if g["status"] == "unknown"]
        scopes = self._build_scopes_doc(obs, pending_tpl_ids, coverage)

        return {
            "device": (cmds[0].device_id if cmds else None) or device or path.stem,
            "file": str(path), "vendor_family": family, "dialect": dialect,
            "new_vendor_family": new_family, "profile_lineage": lineage,
            "registry_version": self.registry_version,
            "summary": {"commands": len(cmds), "matched": len(cmds) - len(unmatched),
                        "mapped": counts["mapped"], "ignored": counts["ignored"],
                        "pending": counts["pending"], "unrecognized": counts["unrecognized"],
                        "new_templates": len(new), "values": len(values),
                        "defaults_applied": n_defaults, "errors": len(errors),
                        "coverage": coverage},
            "values": values,
            "scopes": scopes,
            "coverage": coverage,
            "pending": self._pending(pending),
            "new_template_ids": pending_tpl_ids,
            "template_counts": dict(Counter(t["db_id"] for t in tpl_of.values())),
            "unrecognized": unrecognized, "conflicts": conflicts, "errors": errors,
        }

    def _record(self, obs, conflicts, key, stype, sid, value, t, m, c, merge="union") -> None:
        slot = (key, stype, sid)
        entry = {"key": key, "scope_type": stype, "scope_id": sid, "value": value,
                 "source": "config", "template": t["signature"], "template_id": t["db_id"],
                 "mapping_id": m["id"], "line": c.src_line, "raw": c.raw}
        if self.keys[key]["cardinality"] == "list":
            vals = value if isinstance(value, list) else [value]
            if merge == "append":
                if slot in obs:
                    obs[slot]["value"] += vals
                else:
                    obs[slot] = {**entry, "value": list(vals)}
            elif slot in obs:
                obs[slot]["value"] += [v for v in vals if v not in obs[slot]["value"]]
            else:
                obs[slot] = {**entry, "value": list(dict.fromkeys(vals))}
            return
        if slot in obs and obs[slot]["value"] != value:
            conflicts.append({"key": key, "scope": f"{stype}:{sid}", "was": obs[slot]["value"],
                              "was_line": obs[slot]["line"], "now": value, "line": c.src_line})
        obs[slot] = entry                                  # last statement wins

    def _suggestion(self, tid) -> tuple[dict | None, list[str] | None]:
        """Top suggestion for display, and the keys of every ranked candidate
        (None = the model has not scored this template, so it could emit any key)."""
        s = registry.latest_suggestion(self.conn, tid)
        cands = ((s or {}).get("evidence") or {}).get("candidates")
        if not cands:
            return None, None
        c = cands[0]
        top = {"key": c["key"], "score": c["score"], "preview": c["preview"],
               "emits": (c["proposal"] or {}).get("emits")}
        return top, [x["key"] for x in cands if x["kind"] == "key"]

    def _pending(self, pending) -> list[dict]:
        by_id = {t["db_id"]: t for t in self.templates}
        out = []
        for tid, cs in sorted(pending.items(), key=lambda kv: -len(kv[1])):
            t = by_id.get(tid) or registry.get_template(self.conn, str(tid))
            top, keys = self._suggestion(tid)
            out.append({"template_id": tid, "display": t["display"] if t else "?",
                        "status": "suggested" if top else (t or {}).get("status", "unknown"),
                        "lines": [c.src_line for c in cs[:5]], "example": cs[0].raw,
                        "suggestion": top, "candidate_keys": keys})
        return out

    def refresh_pending(self, doc: dict) -> None:
        """Re-read suggestions for a document's pending templates (after the suggester ran)."""
        for p in doc["pending"]:
            top, keys = self._suggestion(p["template_id"])
            p.update(suggestion=top, candidate_keys=keys,
                     status="suggested" if top else p["status"])
