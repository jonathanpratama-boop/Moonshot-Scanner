"""Migrations and cross-connection work claiming."""
from __future__ import annotations

import json

import pytest

from astra import db, research, work
from astra.core import AstraError

from .conftest import FIX


def test_migrations_idempotent_and_checksum_guarded(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "m.db", "sample")
    assert db.migrate(conn) == []  # nothing pending on re-run
    real = db._migration_files

    def edited():
        return [(v, sql + "\n-- edited") for v, sql in real()]

    monkeypatch.setattr(db, "_migration_files", edited)
    with pytest.raises(AstraError, match="checksum mismatch"):
        db.migrate(conn)


def test_open_requires_init(tmp_path):
    with pytest.raises(AstraError, match="does not exist"):
        db.open_db(tmp_path / "nope.db")


def test_claim_is_exclusive_across_connections(scanned_db, clock):
    research.import_research(scanned_db, json.loads((FIX / "research" / "ZSMPA_initial.json").read_text()))
    path = scanned_db.execute("PRAGMA database_list").fetchone()["file"]
    other = db.connect(path)
    first = work.claim(scanned_db, "proc-1", ("recheck",), 10, 300)
    second = work.claim(other, "proc-2", ("recheck",), 10, 300)
    assert len(first) == 1 and second == []
    other.close()
