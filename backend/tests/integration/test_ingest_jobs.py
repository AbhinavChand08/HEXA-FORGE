"""Task D1: the ingestion job pipeline (raw storage, status lifecycle, vendor ambiguity
gate) end to end, through the `ingest` CLI command."""
from types import SimpleNamespace

from app import settings
from app.cli.mapper import cmd_ingest
from app.registry import repository as registry

CISCO = settings.SAMPLES_DIR / "cisco" / "cis-hardened-running-config.cfg"


def _args(path, **overrides):
    defaults = dict(path=str(path), device=None, uploader="alice", declared_vendor=None,
                    no_learn=False, no_suggest=True, json=None, show=5)
    return SimpleNamespace(**{**defaults, **overrides})


def test_ingest_normalizes_a_recognised_vendor_and_links_the_job(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OBJECT_STORE_DIR", tmp_path)
    job = cmd_ingest(_args(CISCO, device="rtr-01"), conn)
    conn.commit()

    assert job["status"] in ("NORMALIZED", "PARTIALLY_NORMALIZED")
    assert job["vendor_family"] == "cisco" and job["dialect"] == "indent"
    assert [h["status"] for h in job["status_history"]] == \
        ["RECEIVED", "DETECTED", "FLATTENED", job["status"]]

    device = registry.get_device_by_job(conn, job["id"])
    assert device is not None and device["job_id"] == job["id"]


def test_ingest_reuses_the_earlier_job_for_unchanged_bytes(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OBJECT_STORE_DIR", tmp_path)
    args = _args(CISCO, device="rtr-02")
    first = cmd_ingest(args, conn)
    conn.commit()
    before = len(registry.list_jobs(conn, device="rtr-02"))

    second = cmd_ingest(args, conn)               # same bytes, same device: reused, not re-parsed
    conn.commit()

    assert second["id"] == first["id"]
    assert len(registry.list_jobs(conn, device="rtr-02")) == before


def test_ingest_stops_at_unknown_vendor_without_normalizing(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OBJECT_STORE_DIR", tmp_path)
    mystery = tmp_path / "mystery.cfg"
    mystery.write_text("nothing recognisable is here at all\n")

    job = cmd_ingest(_args(mystery, device="mystery-01"), conn)
    conn.commit()

    assert job["status"] == "UNKNOWN_VENDOR"
    assert job["detection"]["status"] == "UNKNOWN_VENDOR"
    assert registry.get_device_by_job(conn, job["id"]) is None       # nothing was normalized


def test_ingest_trusts_a_declared_vendor_and_flags_disagreement(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OBJECT_STORE_DIR", tmp_path)
    job = cmd_ingest(_args(CISCO, device="rtr-03", declared_vendor="cisco"), conn)
    conn.commit()
    assert job["vendor_family"] == "cisco" and job["declared_vendor"] == "cisco"
    assert job["detection"]["mismatch"] is False

    job = cmd_ingest(_args(CISCO, device="rtr-04", declared_vendor="arista"), conn)
    conn.commit()
    assert job["vendor_family"] == "arista"                          # trusted anyway
    assert job["detection"]["mismatch"] is True                      # but flagged
