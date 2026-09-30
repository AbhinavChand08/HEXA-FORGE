"""Paths and connection settings; environment variables override the defaults."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = Path(os.environ.get("SIH_SEED_DIR", ROOT / "registry-seed"))
SAMPLES_DIR = ROOT / "samples" / "configs"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sih:sih@localhost:5432/sih_registry")
# Raw job uploads (design 5.2): local folder stand-in for the object store (MinIO in prod).
OBJECT_STORE_DIR = Path(os.environ.get("SIH_OBJECT_STORE", ROOT / "var" / "raw"))

# ML service (design §12.8, task G1).  Empty = service not configured; the backend
# queues templates without suggestions (P3 "pipeline never blocks").
ML_SERVICE_URL = os.environ.get("SIH_ML_SERVICE_URL", "")
# Per-request timeout (seconds) when calling the ML service.
ML_SERVICE_TIMEOUT = float(os.environ.get("SIH_ML_SERVICE_TIMEOUT", "5.0"))
