"""Engine-integrity verification.

Confirms that the frozen reconstruction engine on disk is byte-identical to
the released, validated version. Run before any analysis whose output will
be cited:

    python -m comettrace.provenance
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import ENGINE_SHA256


def engine_path() -> Path:
    return Path(__file__).with_name("legacy_engine.py")


def compute_engine_sha256() -> str:
    return hashlib.sha256(engine_path().read_bytes()).hexdigest()


def verify(quiet: bool = False) -> bool:
    actual = compute_engine_sha256()
    ok = actual == ENGINE_SHA256
    if not quiet:
        print(f"engine file : {engine_path()}")
        print(f"expected    : {ENGINE_SHA256}")
        print(f"actual      : {actual}")
        print("status      : VERIFIED" if ok else "status      : MISMATCH")
    return ok


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
