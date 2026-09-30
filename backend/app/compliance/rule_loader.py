"""Active rules for an audit (rule sets are task J5; today a benchmark name filters)."""
from __future__ import annotations

from app.registry import repository as registry


def load_rules(conn, benchmark: str | None = None) -> list[dict]:
    return registry.rules(conn, benchmark)
