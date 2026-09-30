"""HITL review: list unknown templates for a device, submit approve/ignore decisions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_db
from app.api.devices import _ingest_one, _job_to_response
from app.mapping.normalizer import Normalizer, validate
from app.registry import repository as registry
from app import settings


def _confidence_decision(score: float) -> str:
    if score >= 0.90:
        return "auto_approve"
    if score >= 0.60:
        return "suggested"
    return "unknown"

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{job_id}/review")
def get_review_queue(job_id: str, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    device = registry.get_device_by_job(conn, job_id)
    if not device:
        return []

    rows = conn.execute(
        """SELECT DISTINCT ON (t.id)
                  t.id, t.display, t.examples, t.frequency, t.status,
                  t.vars, t.slots, t.vendor_family,
                  m.evidence AS model_evidence
           FROM template t
           JOIN job_template jt ON jt.template_id = t.id
           LEFT JOIN mapping m ON m.template_id = t.id
               AND m.source IN ('model', 'auto_ml')
               AND m.version = (
                   SELECT MAX(version) FROM mapping
                   WHERE template_id = t.id AND source IN ('model', 'auto_ml')
               )
           WHERE jt.job_id = %s AND t.status IN ('unknown', 'suggested')
           ORDER BY t.id, t.frequency DESC""",
        (device["id"],)).fetchall()

    result = []
    needs_live_ml: list[tuple[int, int]] = []  # (result_index, template_id)

    for r in rows:
        evidence = r["model_evidence"] or {}
        cands = evidence.get("candidates") or []
        model_version = evidence.get("model_version")
        suggestion = None
        if cands:
            best = cands[0]
            proposal = best.get("proposal") or {}
            emits = proposal.get("emits") or [{}]
            score = best.get("score") or 0.0
            suggestion = {
                "key": best.get("key"),
                "score": score,
                "value_expr": (emits[0] or {}).get("value"),
                "model_version": model_version,
                "decision": _confidence_decision(score),
            }
        result.append({
            "id": r["id"],
            "display": r["display"],
            "examples": (r["examples"] or [])[:3],
            "frequency": r["frequency"],
            "status": r["status"],
            "vars": r["vars"] or [],
            "vendor_family": r["vendor_family"],
            "suggestion": suggestion,
        })
        if not cands:
            needs_live_ml.append((len(result) - 1, r["id"]))

    # For templates without stored ML evidence, call the ML service live
    if needs_live_ml and settings.ML_SERVICE_URL:
        from app import ml_client
        for idx, tpl_id in needs_live_ml:
            resp = ml_client.suggest(tpl_id)
            if resp and resp.get("candidates"):
                best = resp["candidates"][0]
                proposal = best.get("proposal") or {}
                emits = proposal.get("emits") or [{}]
                score = best.get("score") or 0.0
                result[idx]["suggestion"] = {
                    "key": best.get("key"),
                    "score": score,
                    "value_expr": (emits[0] or {}).get("value"),
                    "model_version": resp.get("model_version"),
                    "decision": resp.get("decision", _confidence_decision(score)),
                }

    return result


class ReviewDecision(BaseModel):
    decision: str  # "approve" | "ignore"
    key: str | None = None
    value_expr: str | None = None
    scope_type: str | None = None
    scope_id_expr: str | None = None
    slot_names: list[str] = []
    approved_by: str = "user"


@router.post("/{job_id}/review/{template_id}")
def submit_review(job_id: str, template_id: int, body: ReviewDecision, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    tpl = registry.get_template(conn, str(template_id))
    if not tpl:
        raise HTTPException(404, f"template {template_id} not found")

    if body.decision == "ignore":
        registry.ignore_template(conn, template_id, body.approved_by)
    elif body.decision == "approve":
        if not body.key:
            raise HTTPException(422, "key is required for approve decision")
        emits = [{"key": body.key, "value": body.value_expr or "v1"}]
        scope = ({"type": body.scope_type, "id": body.scope_id_expr or ""}
                 if body.scope_type else None)
        mapping_draft = {"emits": emits, "scope": scope, "slot_names": body.slot_names}
        keys = registry.keys(conn)
        errors, _ = validate(mapping_draft, tpl, keys)
        if errors:
            raise HTTPException(422, {"errors": errors})
        registry.save_mapping(conn, template_id, emits=emits, scope=scope,
                              slot_names=body.slot_names, source="human",
                              approved_by=body.approved_by, active=True)
    else:
        raise HTTPException(422, f"unknown decision {body.decision!r}")

    conn.commit()

    # Re-normalize with updated registry so the new mapping takes effect
    try:
        raw_path = settings.OBJECT_STORE_DIR / job["object_key"]
        n = Normalizer(conn)
        doc = n.run(raw_path, learn=True, count=True, device=job["device"], job_id=job_id)
        registry.save_device(conn, doc, job_id=job_id)
        status = ("NORMALIZED" if not doc["pending"] and not doc["unrecognized"]
                  else "PARTIALLY_NORMALIZED")
        registry.update_job_status(conn, job_id, status)
        conn.commit()
    except Exception as exc:
        log.warning("re-normalization failed after review decision: %s", exc)

    return _job_to_response(conn, registry.get_job(conn, job_id))
