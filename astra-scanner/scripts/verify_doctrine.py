"""Verify a local doctrine bundle against the retained source manifest and frozen copies.

Usage: python scripts/verify_doctrine.py PATH/TO/ASTRA-doctrine-sources-2026-09-30
Reads only; never modifies the bundle.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    bundle = Path(sys.argv[1])
    manifest = json.loads((ROOT / "doctrine" / "frozen" / "SOURCE_MANIFEST.json").read_text())
    ok = True
    for f in manifest["files"]:
        p = bundle / f["path"]
        if not p.exists():
            print(f"MISSING  {f['path']}")
            ok = False
            continue
        good = sha(p) == f["sha256"]
        ok &= good
        print(f"{'OK      ' if good else 'CHANGED '} {f['path']}")
    for name, rel in (("detectors-v1.json", "trading/pilot/detectors-v1.json"), ("v2-rules.json", "trading/live/v2-rules.json")):
        p = bundle / rel
        if p.exists():
            same = sha(p) == sha(ROOT / "doctrine" / "frozen" / name)
            ok &= same
            print(f"{'OK      ' if same else 'CHANGED '} frozen copy {name} vs bundle")
    runtime = sha(ROOT / "astra" / "config" / "detectors-v1.json") == sha(ROOT / "doctrine" / "frozen" / "detectors-v1.json")
    ok &= runtime
    print(f"{'OK      ' if runtime else 'CHANGED '} runtime astra/config/detectors-v1.json vs frozen copy")
    print("ALL MATCH" if ok else "MISMATCH FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
