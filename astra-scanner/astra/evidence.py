"""Frozen evidence snapshots. Inputs are saved (and read back) before any research."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import AstraError, canonical_json, content_hash, new_id, now_str, sha256_text


class RecordStorageUnavailable(AstraError):
    """RECORD_STORAGE_UNAVAILABLE: a frozen record could not be written and read back intact."""


def freeze(conn: sqlite3.Connection, kind: str, run_id: str, symbol: str | None, content: dict,
           is_sample: bool) -> tuple[str, bool]:
    """Insert an immutable snapshot (caller holds the transaction).

    Identical content is stored once and its id reused, so repeated scans of the same
    inputs do not multiply evidence. Returns (evidence_id, newly_created).
    """
    text = canonical_json(content)
    digest = sha256_text(text)
    row = conn.execute(
        "SELECT evidence_id FROM evidence_snapshots WHERE content_hash = ? AND kind = ?", (digest, kind)
    ).fetchone()
    if row:
        return row["evidence_id"], False
    evidence_id = new_id("ev")
    conn.execute(
        "INSERT INTO evidence_snapshots(evidence_id,kind,created_at_utc,run_id,symbol,content_json,content_hash,is_sample) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (evidence_id, kind, now_str(), run_id, symbol, text, digest, 1 if is_sample else 0),
    )
    back = conn.execute("SELECT content_json, content_hash FROM evidence_snapshots WHERE evidence_id = ?",
                        (evidence_id,)).fetchone()
    observed = sha256_text(back["content_json"]) if back else ""
    ok = bool(back) and observed == digest == back["content_hash"]
    conn.execute("INSERT INTO evidence_readbacks VALUES (?,?,?,?)", (evidence_id, now_str(), 1 if ok else 0, observed))
    if not ok:
        raise RecordStorageUnavailable(f"read-back mismatch for evidence {evidence_id}")
    return evidence_id, True


def load(conn: sqlite3.Connection, evidence_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM evidence_snapshots WHERE evidence_id = ?", (evidence_id,)).fetchone()
    if not row:
        raise AstraError(f"unknown evidence {evidence_id}")
    content = json.loads(row["content_json"])
    if content_hash(content) != row["content_hash"]:
        raise AstraError(f"evidence {evidence_id} content hash mismatch")
    return {**dict(row), "content": content}
