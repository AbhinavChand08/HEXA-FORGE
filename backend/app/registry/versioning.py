"""Registry version counter (design 9.1, 9.5, P4).

Every change to registry knowledge (mapping, key, default, profile, rule) inserts one
registry_version row in the caller's transaction; the new version is returned to the
caller and stamped on mapping rows and normalized documents. Templates and learned
routing signatures are observations, not knowledge, so they do not bump it.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

KINDS = ("mapping", "key", "default", "profile", "rule")


def bump(conn, kind: str, ref: str | None = None, actor: str | None = None,
         detail: dict | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown change kind {kind!r}")
    return conn.execute(
        "INSERT INTO registry_version (change_kind, change_ref, actor, detail) "
        "VALUES (%s, %s, %s, %s) RETURNING version",
        (kind, ref, actor, Jsonb(detail) if detail is not None else None)).fetchone()["version"]


def current_version(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM registry_version"
                        ).fetchone()["v"]


def changes(conn, ref: str | None = None, limit: int = 50) -> list[dict]:
    """Newest first; ref filters on change_ref ('template:<id>', 'profile:<name>', ...)."""
    return conn.execute(
        """SELECT version, changed_at, change_kind, change_ref, actor, detail
           FROM registry_version WHERE %s::text IS NULL OR change_ref = %s
           ORDER BY version DESC LIMIT %s""", (ref, ref, limit)).fetchall()
