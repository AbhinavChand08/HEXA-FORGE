"""Device upload, listing, and detail endpoints."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import get_db
from app.ingest import jobs as job_store
from app.ingest.detector import detect_vendor
from app.mapping.normalizer import Normalizer
from app.registry import repository as registry
from app import settings

log = logging.getLogger(__name__)
router = APIRouter()


def _ingest_one(conn, data: bytes, filename: str, device_name: str | None = None) -> dict:
    """Full ingest pipeline for one uploaded config file. Mirrors cmd_ingest in cli/mapper.py."""
    device = device_name or Path(filename).stem
    object_key, digest = job_store.store_raw(device, data)

    reused = registry.find_reusable_job(conn, device, digest)
    if reused:
        return dict(reused)

    job = registry.create_job(conn, job_store.new_id(), device, object_key, digest)
    conn.commit()

    text = data.decode("utf-8", errors="replace")
    profiles = registry.vendor_profiles(conn)
    detection = detect_vendor(text, profiles, None, registry.profile_signatures(conn))
    registry.set_job_detection(conn, job["id"], detection["profile"]["name"],
                               detection["dialect"], detection)

    if detection["status"] in ("NEEDS_VENDOR", "UNKNOWN_VENDOR"):
        registry.update_job_status(conn, job["id"], detection["status"], detail=detection)
        conn.commit()
        return dict(registry.get_job(conn, job["id"]))

    registry.update_job_status(conn, job["id"], "DETECTED")
    conn.commit()

    try:
        raw_path = settings.OBJECT_STORE_DIR / object_key
        n = Normalizer(conn, profiles=profiles)
        doc = n.run(raw_path, learn=True, count=True, device=device, job_id=job["id"])
        registry.update_job_status(conn, job["id"], "FLATTENED")
        registry.save_device(conn, doc, job_id=job["id"])
        status = ("NORMALIZED" if not doc["pending"] and not doc["unrecognized"]
                  else "PARTIALLY_NORMALIZED")
        registry.update_job_status(conn, job["id"], status)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        registry.update_job_status(conn, job["id"], "FAILED", detail={"error": str(exc)})
        conn.commit()
        log.exception("normalizer failed for job %s", job["id"])
        raise

    return dict(registry.get_job(conn, job["id"]))


def _job_to_response(conn, job: dict) -> dict:
    device = registry.get_device_by_job(conn, job["id"])
    unknown_count = 0
    if device:
        row = conn.execute(
            """SELECT count(*) AS n FROM template t
               JOIN job_template jt ON jt.template_id = t.id
               WHERE jt.job_id = %s AND t.status IN ('unknown', 'suggested')""",
            (device["id"],)).fetchone()
        unknown_count = row["n"] if row else 0

    has_audit = bool(conn.execute(
        "SELECT 1 FROM audit_run WHERE job_id = %s LIMIT 1", (job["id"],)).fetchone())

    audit_summary = None
    if has_audit:
        arow = conn.execute(
            "SELECT summary FROM audit_run WHERE job_id = %s ORDER BY id DESC LIMIT 1",
            (job["id"],)).fetchone()
        if arow:
            audit_summary = arow["summary"]

    return {
        "id": job["id"],
        "name": job["device"],
        "vendor": job.get("vendor_family") or "unknown",
        "dialect": job.get("dialect") or "unknown",
        "status": job["status"],
        "unknown_count": unknown_count,
        "has_audit": has_audit,
        "created_at": job["created_at"].isoformat() if job.get("created_at") else None,
        "summary": audit_summary,
    }


@router.post("")
def upload_devices(
    files: list[UploadFile] = File(...),
    conn=Depends(get_db),
):
    results = []
    for f in files:
        data = f.file.read()
        try:
            job = _ingest_one(conn, data, f.filename or "config.cfg")
        except Exception as exc:
            log.exception("ingest failed for %s", f.filename)
            results.append({"name": f.filename, "status": "FAILED", "error": str(exc)})
            continue
        results.append(_job_to_response(conn, job))
    return results


@router.get("")
def list_devices(conn=Depends(get_db)):
    jobs = registry.list_jobs(conn, limit=100)
    return [_job_to_response(conn, j) for j in jobs]


@router.get("/{job_id}")
def get_device(job_id: str, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    resp = _job_to_response(conn, job)
    device = registry.get_device_by_job(conn, job_id)
    if device:
        obs = conn.execute(
            "SELECT * FROM observation WHERE device_id = %s ORDER BY key, scope_type, scope_id",
            (device["id"],)).fetchall()
        resp["observations"] = [dict(o) for o in obs]
    return resp


@router.get("/{job_id}/source")
def get_source(job_id: str, conn=Depends(get_db)):
    job = registry.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, f"device {job_id!r} not found")
    raw_path = settings.OBJECT_STORE_DIR / job["object_key"]
    if not raw_path.exists():
        raise HTTPException(404, "source file not found on disk")
    return {"text": raw_path.read_text(errors="replace"), "filename": Path(job["object_key"]).name}
