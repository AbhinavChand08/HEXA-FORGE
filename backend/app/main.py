"""FastAPI application entry point for SIH 26155 config normalizer and auditor."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.devices import router as devices_router
from app.api.review import router as review_router

log = logging.getLogger(__name__)


def _ingest_samples(conn) -> dict:
    """Ingest every file under SAMPLES_DIR on first startup (when jobs table is empty).
    Errors on individual files are logged and skipped so the server always starts."""
    from app import settings
    from app.api.devices import _ingest_one

    row = conn.execute("SELECT count(*) AS n FROM job").fetchone()
    if row["n"] > 0:
        return {"skipped": "already seeded", "jobs": row["n"]}

    samples_dir = settings.SAMPLES_DIR
    if not samples_dir.is_dir():
        log.warning("SAMPLES_DIR %s not found — skipping sample ingestion", samples_dir)
        return {"skipped": "samples dir not found"}

    files = sorted(p for p in samples_dir.rglob("*") if p.is_file())
    if not files:
        return {"skipped": "no sample files"}

    done, failed = 0, 0
    for path in files:
        try:
            data = path.read_bytes()
            # Use vendor/filename as the device name (e.g. "cisco/cis-hardened-running-config")
            device_name = f"{path.parent.name}/{path.stem}"
            _ingest_one(conn, data, path.name, device_name=device_name)
            done += 1
            log.info("sample ingested: %s", path.name)
        except Exception as exc:
            failed += 1
            log.warning("sample ingest failed for %s: %s", path.name, exc)

    return {"ingested": done, "failed": failed, "total": len(files)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.session import connect
    from app.registry.seed_loader import api_seed
    log.info("applying schema and seeding registry …")
    with connect() as conn:
        seed_result = api_seed(conn)
        conn.commit()
        log.info("registry seeded: %s", seed_result)
        log.info("ingesting sample configs …")
        sample_result = _ingest_samples(conn)
        conn.commit()
        log.info("sample ingestion: %s", sample_result)
    yield


app = FastAPI(
    title="Hexa force — Config Normalizer & Auditor",
    version="0.6.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(devices_router, prefix="/api/v1/devices", tags=["devices"])
app.include_router(review_router, prefix="/api/v1/devices", tags=["review"])
app.include_router(audit_router, prefix="/api/v1", tags=["audit"])


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}
