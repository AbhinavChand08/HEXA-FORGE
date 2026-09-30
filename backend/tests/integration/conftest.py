"""Registry tests against a real PostgreSQL + pgvector.

The server is SIH_TEST_DATABASE_URL (only CREATE DATABASE rights are needed; each test
gets its own database) or a throwaway pgserver; without either the tests are skipped.
The registry is seeded once into a template database and cloned per test.
"""
import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app import settings
from app.db.session import connect
from app.registry import seed_loader


def _admin(url):
    return psycopg.connect(url, autocommit=True)


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    url = os.environ.get("SIH_TEST_DATABASE_URL")
    if url:
        return url
    pgserver = pytest.importorskip("pgserver")
    return pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop").get_uri()


@pytest.fixture(scope="session")
def new_db(server_url):
    """new_db(template=None) -> DSN of a fresh database (dropped at the end of the session)."""
    made = []

    def make(template: str | None = None) -> str:
        name = f"sih_test_{uuid.uuid4().hex[:10]}"
        with _admin(server_url) as c:
            c.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
        made.append(name)
        return make_conninfo(server_url, dbname=name)

    yield make
    with _admin(server_url) as c:
        for name in made:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def seeded_template(new_db):
    os.environ["SIH_EMBEDDER"] = "hash"
    dsn = new_db()
    with connect(dsn) as conn:
        seed_loader.init(conn)
        seed_loader.import_templates(conn, settings.SEED_DIR / "learned" / "templates.json")
        seed_loader.seed_mappings(conn)
        conn.commit()
    return conninfo_to_dict(dsn)["dbname"]


@pytest.fixture
def conn(new_db, seeded_template):
    """A connection to a fresh copy of the seeded registry."""
    with connect(new_db(seeded_template)) as c:
        yield c
