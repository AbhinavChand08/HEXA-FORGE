"""Compliance audit endpoints: run audit, retrieve results, download report."""
from __future__ import annotations

import logging
import re
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.api.deps import get_db
from app.compliance.evaluator import evaluate
from app.compliance.rule_loader import load_rules
from app.registry import repository as registry
from app.reporting.builder import text_report

log = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Fix command derivation from template + mapping data
# ---------------------------------------------------------------------------
_SLOT_PAT = re.compile(r'<([A-Z][A-Z0-9]*)(\.\.\.)?>')


def _display_to_cli(display: str) -> str:
    """'parent > child' display → indented CLI command string."""
    parts = display.split(' > ')
    if len(parts) == 1:
        return parts[0]
    return parts[0] + '\n ' + '\n '.join(parts[1:])


def _apply_no(display: str) -> str:
    """Prepend 'no' to a display string.

    For multi-level commands the 'no' targets the last level only so that
    the parent context (e.g. 'line vty 0 4') stays intact:
        line vty 0 4 > transport input telnet
        →  line vty 0 4
            no transport input telnet
    """
    parts = display.split(' > ')
    if len(parts) == 1:
        return 'no ' + parts[0]
    return parts[0] + '\n no ' + '\n '.join(parts[1:])


def _build_fix(display: str, vars_: list, emits: list, slot_names: list,
               rule_key: str, operator: str, expected, actual) -> str | None:
    """
    Derive a remediation CLI command from a template display + mapping emits.

    Covers:
      - Boolean 'not negated' emits: return the command as-is or with 'no'.
      - Single-slot emits: substitute the expected value at the right placeholder.
      - 'not_contains' violations: negate the command using the actual offending value(s).
      - Absent-key 'present' rules: fill all slots with sample values.
      - Complex multi-slot formulas: return None (cannot be inverted generically).
    """
    emit = next((e for e in (emits or []) if e.get('key') == rule_key), None)
    if not emit:
        return None

    value_expr = emit.get('value', '')
    n = len(vars_ or [])

    # Map every variable name to its 0-based slot index (v1..vN + named aliases)
    name_to_pos: dict[str, int] = {f'v{i + 1}': i for i in range(n)}
    for i, name in enumerate(slot_names or []):
        if name:
            name_to_pos[name] = i

    # Default fill: sample value from template vars for each placeholder
    ph_count = len(_SLOT_PAT.findall(display))
    slot_fill = [str((vars_[i] if i < n else {}).get('sample', ''))
                 for i in range(ph_count)]

    def _render(fills: list[str]) -> str:
        it = iter(fills)
        return _display_to_cli(_SLOT_PAT.sub(lambda _: next(it, ''), display))

    def _render_no(fills: list[str]) -> str:
        it = iter(fills)
        return _apply_no(_SLOT_PAT.sub(lambda _: next(it, ''), display))

    # Case A: boolean toggle via 'not negated'
    if 'negated' in value_expr:
        cmd = _render(slot_fill)
        return cmd if expected is True else 'no ' + cmd

    # Identify which slot variables the emit expression reads
    try:
        from app.mapping.transforms.dsl import free_names
        RESERVED = {'negated', 'present', 'tokens', 'raw', 'values', 'blob'}
        names_used = free_names(value_expr) - RESERVED
    except Exception:
        return None

    slot_positions = sorted({name_to_pos[nm] for nm in names_used if nm in name_to_pos})

    # Case B: constant literal or blob (no invertible slot) — show sample for 'present' rules
    if not slot_positions:
        return _render(slot_fill) if operator == 'present' else None

    # Case C: single-slot emit
    if len(slot_positions) == 1:
        pos = slot_positions[0]

        # not_contains violation: negate the command for each offending actual value
        if operator == 'not_contains' and actual is not None:
            bad_list = actual if isinstance(actual, list) else [actual]
            # Keep only the values that are actually in the disallowed set
            disallowed = set(expected) if isinstance(expected, list) else {expected}
            offending = [v for v in bad_list if v in disallowed] or bad_list
            lines = []
            for bad_val in offending:
                fill = list(slot_fill)
                fill[pos] = str(bad_val)
                lines.append(_render_no(fill))
            return '\n'.join(lines)

        # All other single-slot cases: substitute expected value (keep sample when absent)
        if expected is not None:
            exp_val = expected[0] if isinstance(expected, list) else expected
            slot_fill[pos] = str(exp_val)

        return _render(slot_fill)

    # Case D: complex multi-slot formula (e.g. "min * 60 + sec") — cannot invert
    return None


def _compute_fixes(conn, device_id: int, vendor_family: str, results: list) -> dict:
    """
    For every FAIL result, derive a remediation command from the observation's
    template display + mapping emits.

    Returns {(rule_id, scope_instance): fix_str}.
    """
    if not results:
        return {}

    # Load template + mapping data for all config-sourced observations on this device
    rows = conn.execute(
        """SELECT o.key, o.scope_type, o.scope_id, o.value,
                  t.display, t.vars, m.emits, m.slot_names
           FROM observation o
           JOIN template t ON t.id = o.template_id
           JOIN mapping  m ON m.id = o.mapping_id
           WHERE o.device_id = %s AND o.source = 'config'""",
        (device_id,)).fetchall()

    obs_data: dict[tuple, dict] = {}
    for row in rows:
        obs_data[(row['key'], row['scope_type'], row['scope_id'])] = row

    fixes: dict[tuple, str] = {}
    for r in results:
        if r.get('status') != 'FAIL':
            continue

        rule_id = r.get('rule') or r.get('rule_id') or ''
        scope = r.get('scope') or r.get('scope_instance') or ''
        can_key = r.get('key') or ''
        operator = r.get('operator') or ''
        expected = r.get('expected')
        actual = r.get('actual')

        # Decompose scope_instance → (scope_type, scope_id)
        if ':' in scope:
            scope_type, scope_id = scope.split(':', 1)
        else:
            scope_type, scope_id = 'GLOBAL', ''

        obs = obs_data.get((can_key, scope_type, scope_id))
        if obs:
            fix = _build_fix(obs['display'], obs['vars'], obs['emits'], obs['slot_names'],
                             can_key, operator, expected, actual)
        else:
            # Key is absent: find a representative template for this vendor
            fix = _fix_for_absent_key(conn, vendor_family, can_key, operator, expected)

        if fix:
            fixes[(rule_id, scope)] = fix

    return fixes


def _fix_for_absent_key(conn, vendor_family: str, can_key: str,
                        operator: str, expected) -> str | None:
    """Find the most-used template for vendor_family that emits can_key; return sample command."""
    row = conn.execute(
        """SELECT t.display, t.vars, m.emits, m.slot_names
           FROM template t
           JOIN mapping m ON m.template_id = t.id AND m.active = true
           WHERE t.vendor_family = %s
             AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements(m.emits) e
                 WHERE e->>'key' = %s
             )
           ORDER BY t.frequency DESC
           LIMIT 1""",
        (vendor_family, can_key)).fetchone()
    if not row:
        return None
    return _build_fix(row['display'], row['vars'], row['emits'], row['slot_names'],
                      can_key, operator, expected, None)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@router.get("/ontology/keys")
def list_canonical_keys(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT key, type, category, description FROM canonical_key ORDER BY category, key"
    ).fetchall()
    return [{"key": r["key"], "type": r["type"], "category": r["category"],
             "description": r["description"]} for r in rows]


@router.get("/benchmarks")
def list_benchmarks(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT benchmark, count(*) AS rule_count FROM rule GROUP BY benchmark ORDER BY benchmark"
    ).fetchall()
    return [{"name": r["benchmark"], "rule_count": r["rule_count"]} for r in rows]


class AuditRequest(BaseModel):
    benchmarks: list[str] = ["baseline"]


@router.post("/devices/{job_id}/audit")
def run_audit(job_id: str, body: AuditRequest, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    device = registry.get_device_by_job(conn, job_id)
    if not device:
        raise HTTPException(422, "device has not been normalized yet")

    obs = conn.execute(
        "SELECT * FROM observation WHERE device_id = %s", (device["id"],)).fetchall()

    pending_rows = conn.execute(
        """SELECT DISTINCT ON (t.id)
                  t.id, t.status, m.evidence AS model_evidence
           FROM template t
           JOIN job_template jt ON jt.template_id = t.id
           LEFT JOIN mapping m ON m.template_id = t.id
               AND m.source IN ('model', 'auto_ml')
               AND m.version = (
                   SELECT MAX(version) FROM mapping
                   WHERE template_id = t.id AND source IN ('model', 'auto_ml')
               )
           WHERE jt.job_id = %s AND t.status IN ('unknown', 'suggested')
           ORDER BY t.id""",
        (device["id"],)).fetchall()

    pending = []
    for r in pending_rows:
        evidence = r["model_evidence"] or {}
        cands = evidence.get("candidates") or []
        candidate_keys = [c["key"] for c in cands if c.get("kind") == "key"] if cands else None
        pending.append({"template_id": r["id"], "candidate_keys": candidate_keys})

    doc = {
        "device": device["name"],
        "vendor_family": device["vendor_family"],
        "file": device["source_file"] or "",
        "registry_version": device["registry_version"],
        "values": [dict(o) for o in obs],
        "pending": pending,
        "summary": device["summary"] or {},
    }

    all_results = []
    for benchmark in body.benchmarks:
        rules = load_rules(conn, benchmark)
        all_results.extend(evaluate(doc, rules))

    summary = _summarize(all_results)
    rv = registry.current_version(conn)

    fixes = _compute_fixes(conn, device["id"], device["vendor_family"] or "", all_results)

    run_id = conn.execute(
        """INSERT INTO audit_run (job_id, benchmarks, registry_version, finished_at, summary)
           VALUES (%s, %s, %s, now(), %s) RETURNING id""",
        (job_id, body.benchmarks, rv, Jsonb(summary))).fetchone()["id"]

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO rule_result
                   (audit_run_id, rule_id, scope_instance, status, actual, expected,
                    severity, title, key, operator, source, src_line, evidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(run_id,
              r["rule"], r.get("scope") or "", r["status"],
              Jsonb(r.get("actual")), Jsonb(r.get("expected")),
              r.get("severity"), r.get("title"),
              r.get("key"), r.get("operator"),
              r.get("source"), r.get("line"),
              Jsonb({"pending": r.get("pending", []), "unscored": r.get("unscored", 0)}))
             for r in all_results])

    registry.update_job_status(conn, job_id, "AUDITED")
    conn.commit()
    return _audit_response(run_id, body.benchmarks, all_results, rv, summary, fixes)


@router.get("/devices/{job_id}/audit")
def get_audit(job_id: str, conn=Depends(get_db)):
    if not registry.get_job(conn, job_id):
        raise HTTPException(404, f"device {job_id!r} not found")
    run = conn.execute(
        "SELECT * FROM audit_run WHERE job_id = %s ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    if not run:
        raise HTTPException(404, "no audit found for this device; run POST /audit first")
    results = conn.execute(
        "SELECT * FROM rule_result WHERE audit_run_id = %s ORDER BY status, severity",
        (run["id"],)).fetchall()

    device = registry.get_device_by_job(conn, job_id)
    fixes: dict = {}
    if device:
        fixes = _compute_fixes(conn, device["id"], device["vendor_family"] or "",
                               [dict(r) for r in results])

    return _audit_response(run["id"], run["benchmarks"], [dict(r) for r in results],
                           run["registry_version"], run["summary"], fixes)


@router.get("/devices/{job_id}/audit/report")
def get_report(job_id: str, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    device = registry.get_device_by_job(conn, job_id)
    run = conn.execute(
        "SELECT * FROM audit_run WHERE job_id = %s ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    if not run:
        raise HTTPException(404, "no audit found for this device; run POST /audit first")
    results = conn.execute(
        "SELECT * FROM rule_result WHERE audit_run_id = %s ORDER BY status, severity",
        (run["id"],)).fetchall()

    report_doc = {
        "device": device["name"] if device else job["device"],
        "vendor_family": device["vendor_family"] if device else (job.get("vendor_family") or ""),
        "file": device["source_file"] if device else "",
        "registry_version": run["registry_version"],
    }

    report_results = []
    for r in results:
        evidence = r["evidence"] or {}
        report_results.append({
            "rule": r["rule_id"],
            "status": r["status"],
            "actual": r["actual"],
            "expected": r["expected"],
            "severity": r["severity"] or "medium",
            "title": r["title"] or r["rule_id"],
            "key": r["key"] or "",
            "scope": r["scope_instance"] or "",
            "operator": r["operator"] or "eq",
            "source": r["source"] or "",
            "line": r["src_line"],
            "pending": evidence.get("pending", []),
            "unscored": evidence.get("unscored", 0),
        })

    return {
        "device": report_doc["device"],
        "benchmarks": run["benchmarks"],
        "summary": run["summary"],
        "text": text_report(report_doc, report_results),
        "results": report_results,
    }


def _summarize(results: list[dict]) -> dict:
    counts = Counter(r["status"] for r in results)
    total = len(results)
    passed = counts.get("PASS", 0)
    decided = total - counts.get("UNKNOWN", 0) - counts.get("ERROR", 0)
    return {
        "PASS": counts.get("PASS", 0),
        "FAIL": counts.get("FAIL", 0),
        "UNKNOWN": counts.get("UNKNOWN", 0),
        "ERROR": counts.get("ERROR", 0),
        "total": total,
        "score": round(passed / decided * 100) if decided > 0 else None,
    }


def _audit_response(run_id, benchmarks, results, registry_version,
                    summary=None, fixes: dict | None = None) -> dict:
    fixes = fixes or {}
    return {
        "id": run_id,
        "benchmarks": benchmarks,
        "registry_version": registry_version,
        "summary": summary or _summarize(results),
        "results": [
            {
                "rule": r.get("rule") or r.get("rule_id"),
                "title": r.get("title"),
                "key": r.get("key"),
                "status": r["status"],
                "severity": r.get("severity"),
                "actual": r.get("actual"),
                "expected": r.get("expected"),
                "scope": r.get("scope") or r.get("scope_instance", ""),
                "fix": fixes.get(
                    (r.get("rule") or r.get("rule_id"),
                     r.get("scope") or r.get("scope_instance") or "")
                ),
            }
            for r in results
        ],
    }
