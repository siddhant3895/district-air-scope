"""SQLite database connection manager for DistrictWatch."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator
from app.config import get_settings


def resolve_sqlite_path(database_url: str | None = None) -> str:
    """Resolve a SQLAlchemy-style sqlite:/// URL or file path to a raw sqlite3 path."""
    url = database_url or get_settings().database_url
    if url == "sqlite:///:memory:" or url == ":memory:":
        return ":memory:"
    if url.startswith("sqlite:///"):
        raw_path = url[len("sqlite:///") :]
        path = Path(raw_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    return url


def create_connection(database_url: str | None = None) -> sqlite3.Connection:
    """Create a sqlite3 connection with foreign keys and Row factory enabled."""
    db_path = resolve_sqlite_path(database_url)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def get_db_connection(database_url: str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a transactional sqlite3 connection."""
    conn = create_connection(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
