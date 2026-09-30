"""Build a clean transfer package: application, fixtures, docs and handoff.

Excludes virtual environments, databases, outboxes, caches, build output, .env files and
the local (private) doctrine bundle. Writes dist/astra-scanner-transfer-<UTC date>.zip with
PACKAGE_MANIFEST.json (path, bytes, SHA-256 for every file) inside.

Usage: python scripts/package_transfer.py
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".venv", "var", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git", "outbox", "build"}
EXCLUDE_PREFIXES = ("doctrine/local",)
EXCLUDE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".pyc")
EXCLUDE_NAMES = {".env"}


def included(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts):
        return False
    s = rel.as_posix()
    if s.startswith(EXCLUDE_PREFIXES) or rel.name in EXCLUDE_NAMES or s.endswith(EXCLUDE_SUFFIXES):
        return False
    return True


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = ROOT / "dist" / f"astra-scanner-transfer-{stamp}.zip"
    out.parent.mkdir(exist_ok=True)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p.relative_to(ROOT)))
    manifest = {"created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": []}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            data = p.read_bytes()
            manifest["files"].append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            z.writestr(f"astra-scanner/{rel}", data)
        z.writestr("astra-scanner/PACKAGE_MANIFEST.json", json.dumps(manifest, indent=1))
    print(f"{out} ({len(files)} files, {out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
